# queue_mgr.py - Flood protection, with an isolated VIP queue
import time
import sys
import builtins
import socket
import defaults as config
import runtime
import stats_mgr

# The ordinary flood-protection queue
config.send_queue = {}


def next_standard_line(send_queue, last_served):
    """Pop ONE line from the standard lane: the first user after `last_served`
    (in queue order, wrapping round) who has something waiting. Returns
    (user, line), or None when nobody does.

    The cursor is the caller's: it passes the user it served last time and
    remembers the one returned here. A cursor rather than "every user, every
    pass" is what #527 is about - see queue_worker() - and it keeps the
    per-user fairness #426 introduced, spread over passes instead of packed
    into one.

    A user whose LAST line goes out is left in the dict, empty, rather than
    deleted on the spot: the cursor is their name, and deleting them would
    lose its place - the next call would start from the top and the first
    users in the dict would be served twice before the last was served once.
    The empty entry is tidied away when the cursor next passes it, one
    rotation later. A user who has gone entirely (a rehash reset, say) means
    "start from the top", which is the only place left to start.
    """
    # Under the lock the producers take (#665): the get-falsy-pop below and
    # queue_message()'s not-in-create-append are each three steps, and an
    # interleaving lost the line just appended (it landed on the list this
    # was about to drop with the key) or raised KeyError in the producer.
    with runtime.send_queue_lock:
        users = builtins.list(send_queue.keys())
        if not users:
            return None
        start = users.index(last_served) + 1 if last_served in users else 0
        for user in users[start:] + users[:start]:
            lines = send_queue.get(user)
            if lines:
                return user, lines.pop(0)
            send_queue.pop(user, None)
        return None

def queue_worker():
    """Flood-protection worker, with a separate express lane for searches and adverts."""
    print("[QUEUE] Isolate-Priority flood protection worker started.")

    # The standard lane's cursor (#527): who was served last pass, so the next
    # pass starts with the user after them. A local, on purpose - it is the
    # worker's own bookkeeping and has no meaning outside this loop.
    last_served = None

    while True:
        try:
            # FIXED: the socket lookup used to sit OUTSIDE the try block. Anything
            # that can raise has to be inside it, or the pump dies silently.
            oserve_mod = sys.modules.get('oserve')
            current_sock = None
            if oserve_mod and hasattr(oserve_mod, 'irc_connection'):
                current_sock = oserve_mod.irc_connection

            # ---------------------------------------------------------------------
            # QUEUE CAP: while the bot is disconnected nothing can be sent, so both
            # queues grow without bound. Drop the oldest lines so a long outage
            # cannot eat all the memory.
            # ---------------------------------------------------------------------
            max_vip = getattr(config, 'MAX_VIP_QUEUE', 200)
            if hasattr(config, 'vip_queue') and len(config.vip_queue) > max_vip:
                dropped = len(config.vip_queue) - max_vip
                del config.vip_queue[:dropped]
                print(f"[QUEUE CAP] Dropped {dropped} old VIP lines (cap is {max_vip}).")

            max_user = getattr(config, 'MAX_USER_SEND_QUEUE', 100)
            with runtime.send_queue_lock:
                for q_user in list(config.send_queue.keys()):
                    if len(config.send_queue.get(q_user, [])) > max_user:
                        q_dropped = len(config.send_queue[q_user]) - max_user
                        del config.send_queue[q_user][:q_dropped]
                        print(f"[QUEUE CAP] Dropped {q_dropped} old lines for {q_user} (cap is {max_user}).")

            # FIXED: hold everything while the bot is offline instead of draining into
            # a void. Both lanes below pop BEFORE testing `if current_sock:`, so once
            # irc.py started clearing oserve.irc_connection on close, every message
            # popped during a reconnect was silently discarded - search results, queue
            # notices and adverts alike, with no error anywhere. Waiting here keeps the
            # queues intact until a live socket exists; the caps above stop them growing
            # without bound during a long outage.
            #
            # TWO GATES, like the debug drain in announce.py (#630). irc.py
            # publishes oserve.irc_connection straight after connect(), before
            # NICK/USER have gone out and seconds before the JOINs land - so
            # the socket alone let whatever the last connection left behind
            # (a "Sent:" notice, queue positions, a rejoin) drain into a
            # window the server answers with 451 and 404, and the lines were
            # gone without a word. activation_triggered is set once every
            # target channel has answered its JOIN - or the watchdog gave up
            # waiting - and cleared by the disconnect epilogue; it is not the
            # channel-sync flag, which stays False on a connection that never
            # got into a channel and would then hold the very JOIN that asks
            # to be let back in.
            if not current_sock or not getattr(config, 'activation_triggered', False):
                time.sleep(0.5)
                continue

            # ---------------------------------------------------------------------
            # sendall(), not send() (#456). send() returns how many bytes it
            # actually took, and both lanes discarded that number - so on
            # Linux, with the socket's kernel send buffer within a few hundred
            # bytes of full, a short write truncated an IRC line mid-message
            # and the server read whatever arrived as a complete command.
            # announce.py's debug drain has used sendall() for exactly this
            # reason; these two were the last places that did not. The same
            # encode arguments too: a filename the socket cannot spell must
            # drop a character rather than raise on the worker thread.
            # EXPRESS LANE (priority 1): drain one VIP line first, if there is one.
            #
            # #426: this used to `continue` straight back to the top after every
            # VIP send, skipping the standard lane below entirely for as long as
            # ANYTHING remained in vip_queue - which is strict priority with no
            # aging, not "priority". oserve.queue_message() put every is_vip=True
            # reply in this lane, including per-user command replies (-help alone
            # queues 5 lines per request), and is_flooding() permits 10 commands
            # per 5s per nick - so ONE user well inside the ordinary flood limit
            # could inject VIP lines faster than MSG_DELAY drains them, peg
            # vip_queue at its MAX_VIP_QUEUE cap indefinitely, and starve
            # config.send_queue completely: @find results, "Preparing full list"
            # and queue-position notices stopped reaching ANYONE, silently.
            #
            # No `continue` now: at most one VIP line is sent here, and the
            # standard lane below always gets its own turn in the SAME pass
            # rather than only when VIP happens to run dry. VIP still drains as
            # fast as before whenever it has a backlog - this does not slow it
            # down - it just no longer does so at the standard lane's total
            # exclusion.
            # ---------------------------------------------------------------------
            if hasattr(config, 'vip_queue') and config.vip_queue:
                msg = config.vip_queue.pop(0)
                # Shared with announce.py's debug drain - see runtime.OutboundPacer.
                # Reserved before the send, not slept after it, so a message that
                # fails to send still costs its slot rather than letting a broken
                # pipe retry in a tight loop.
                runtime.outbound_pacer.wait_for_slot(config.MSG_DELAY)
                try:
                    if current_sock:
                        current_sock.sendall(msg.encode("utf-8", errors="ignore"))
                        if getattr(config, 'DEBUG_MODE', False):
                            print(f"[RAW OUT VIP] {msg.strip()}")
                except socket.error as net_err:
                    # FIXED: this used to be a "break". It broke out of while True:,
                    # the thread returned, and because queue_worker is started ONCE
                    # in oserve.py with nothing supervising it, the bot's only
                    # outbound message pump was dead for the rest of the process's
                    # life. The bot reconnected and looked healthy in the channel
                    # while nothing sent through queue_message ever went out again.
                    print(f"[QUEUE NET ERROR] Connection is broken ({net_err}). Clearing VIP and waiting for a new socket.")
                    del config.vip_queue[:]
                    time.sleep(1.0)
                    continue
            # ---------------------------------------------------------------------

            # STANDARD LANE: ONE line per pass, rotating through the users (#527).
            #
            # #426 replaced VIP's `continue` with "one VIP line, then one line
            # for EVERY user with a backlog" - and under load that turns the
            # starvation round the other way. Each line costs a MSG_DELAY slot
            # on the shared pacer (5 s by default), so with N users spamming a
            # pass was 1 VIP line + N standard lines and the VIP lane - where
            # "Sent:", the advert and "Sending:" live - got one slot in N+1.
            # Seen live: "Sent: doesn't send to the channels before the queue
            # is empty", while the debug drain, on its own thread, kept up.
            #
            # Strict alternation instead: one VIP line, one standard line. VIP
            # is then never more than two of THIS WORKER's slots away whatever
            # the load - and since the shared clock serves its waiters in
            # arrival order (#655), never more than one slot per other lane
            # (the debug drain, a !ping, a DCC ACCEPT) behind that; it used
            # to lose each of those slots by coin toss. The standard lane
            # keeps its per-user fairness across passes through
            # the cursor rather than within one pass. Total throughput is the
            # pacer's either way; only the SHARE changes, and only while VIP
            # has a backlog, which it normally does not.
            picked = next_standard_line(config.send_queue, last_served)
            if picked is not None:
                user, msg = picked
                last_served = user
                # See the VIP lane above: same shared clock.
                runtime.outbound_pacer.wait_for_slot(config.MSG_DELAY)
                try:
                    if current_sock:
                        current_sock.sendall(msg.encode("utf-8", errors="ignore"))
                        if getattr(config, 'DEBUG_MODE', False):
                            print(f"[RAW OUT] {msg.strip()}")
                except socket.error as net_err:
                    print(f"[QUEUE NET ERROR] Connection is broken ({net_err}).")
                except Exception as e:
                    print(f"[ERROR] Failed to send queued message: {e}")

        except Exception as queue_err:
            print(f"[ERROR] Error inside queue worker loop: {queue_err}")
            time.sleep(1)

        # Keep the live transfer rate current. The advert used to be the only
        # thing sampling it, and it fires every ANNOUNCE_INTERVAL - 300 seconds
        # by default - which is fine for a channel line and useless for a
        # dashboard tile. This loop is the daemon's heartbeat, so sampling from
        # here keeps the figure about a second old for every reader.
        #
        # Nearly free: live_speed() caches for a second, so all but one call per
        # second is a timestamp comparison and a return.
        try:
            stats_mgr.live_speed()
        except Exception as speed_err:
            print(f"[QUEUE] Live speed sample failed: {speed_err}")

        time.sleep(0.1)
