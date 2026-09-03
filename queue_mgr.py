# queue_mgr.py - Flood protection, with an isolated VIP queue
import time
import sys
import builtins
import socket
import defaults as config
import stats_mgr

# The ordinary flood-protection queue
config.send_queue = {}

def queue_worker():
    """Flood-protection worker, with a separate express lane for searches and adverts."""
    print("[QUEUE] Isolate-Priority flood protection worker started.")

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
            if not current_sock:
                time.sleep(0.5)
                continue

            # ---------------------------------------------------------------------
            # EXPRESS LANE (priority 1): drain the separate VIP list first.
            # ---------------------------------------------------------------------
            if hasattr(config, 'vip_queue') and config.vip_queue:
                msg = config.vip_queue.pop(0)
                try:
                    if current_sock:
                        current_sock.send(msg.encode())
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

                time.sleep(config.MSG_DELAY)
                continue  # Straight back up to check for more VIP data
            # ---------------------------------------------------------------------

            # STANDARD LANE (priority 2): with VIP empty, work the ordinary queue (round-robin)
            if config.send_queue:
                active_users = builtins.list(config.send_queue.keys())

                for user in active_users:
                    if user in config.send_queue and config.send_queue[user]:
                        msg = config.send_queue[user].pop(0)

                        try:
                           if current_sock:
                               current_sock.send(msg.encode())
                               if getattr(config, 'DEBUG_MODE', False):
                                   print(f"[RAW OUT] {msg.strip()}")
                        except socket.error as net_err:
                           print(f"[QUEUE NET ERROR] Connection is broken ({net_err}).")
                           break
                        except Exception as e:
                           print(f"[ERROR] Failed to send queued message: {e}")

                        time.sleep(config.MSG_DELAY)

                    if user in config.send_queue and not config.send_queue[user]:
                        del config.send_queue[user]

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
