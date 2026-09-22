; =====================================================================
;  dccore.mrc - the bot's whole life in one mIRC window
; =====================================================================
;
;  What it does
;    Opens the admin DCC chat to your DCCore bot, logs in by itself, and
;    draws everything the bot reports in one custom window, @DCCore:
;    the feed (requests, queue, sends, failures, searches, bans, joins)
;    coloured per kind, a side panel with what is sending and who is
;    waiting, and a title bar with the slots, the queue and today's
;    totals. Anything you type in the window goes back to the bot as a
;    console command, and the reply comes back into the window.
;
;  Install
;    Save this file anywhere (your mIRC folder is fine), then in mIRC:
;
;        /load -rs dccore.mrc
;        /dccore pair MusicBot         (MusicBot = your bot's nick)
;
;    The window opens and the bot asks for the password: type it in the
;    window ONCE. The script then asks the bot for a login token of its
;    own, keeps it in dccore.ini next to this file, and from then on
;    connects and logs in without you. The token opens the console and
;    nothing else - it cannot open the web dashboard - and
;    /dccore unpair revokes it at any time.
;
;    dccore.ini is CLEAR TEXT (#683): mIRC's hash-table save writes the
;    token readable, beside this file. Treat that file as you would a
;    password file - it is what a copied mIRC folder or a shared PC
;    gives away, not the .mrc - and /dccore unpair the moment you think
;    it has travelled: the bot then refuses that token for good.
;
;    Then:   /dccore              the command list
;            /dccore options      what to show, in which colour
;
;  Requirements
;    mIRC 6.10 or later. Everything used here dates from mIRC 6.x or
;    earlier: custom windows with an editbox and a side listbox
;    (/window -el), /aline -l, /titlebar, on CHAT with ^ to halt the
;    default text, on CHATCLOSE, hash tables with /hsave and /hload,
;    /hinc and /hdel -w, /timer -m, dialog tables with combo, check and
;    edit, $round, $regex, $duration, $qt, $base, and for the window
;    background /background, /bset and /bwrite (a small .bmp of the
;    chosen colour: mIRC has no per-window background colour, only a
;    per-window picture). Nothing from
;    mIRC 7 (no $json, no UTF-8 switches): the wire is plain ASCII, the
;    bot has already turned control characters into spaces, and the only
;    characters above 127 this script draws are the middle dot and the
;    non-breaking space, which every Windows ANSI code page has.
;
;    A bot that answers `hello` - the DCCore this script ships with, or a
;    later one. An older bot answers "Unknown command" and the script drops to plain mode,
;    where the window simply shows the chat as it comes - still coloured
;    by the bot's own theme, still a console, just no panel.
;
;  The protocol is in docs/ADMIN-CONSOLE.md ("The structured feed, for a
;  script"). Ships with DCCore under the same licence.
;
;  Notes for anyone editing this
;    - Every alias is global (no -l): timers run outside the script's
;      scope and cannot reach a local alias. They are all namespaced
;      dccore.* so they collide with nothing.
;    - Text passed through $1- has its runs of spaces collapsed, so all
;      column padding is done with $chr(160), a non-breaking space.
;    - A ";" begins a comment only at the start of a line, and [ ] are
;      evaluation brackets, so a literal bracket is $chr(91)/$chr(93).
;    - Newer mIRC: the floor stays 6.10 for everything the window does.
;      Where a 7.x identifier adds something real, gate it with
;      if ($version >= 7) { } rather than raising the floor. mIRC 7
;      already decodes the chat as UTF-8, so non-ASCII file names show
;      correctly there without any change here.
; =====================================================================

; ---------------------------------------------------------------------
;  Settings and state
;    dccore       persisted to dccore.ini beside this file
;    dccore.live  this session only: slots, queue, counters
; ---------------------------------------------------------------------

alias dccore.ini { return $qt($+($scriptdir,dccore.ini)) }
alias dccore.bot { return $hget(dccore,bot) }
alias dccore.ver { return 1.1 }
;  The feed's protocol minor this script was written for. The bot says
;  its own in HELLO as major.minor; a different minor means a field was
;  inserted on one side and the lines would read wrong - see HELLO below.
alias dccore.protominor { return 1 }
alias dccore.win { return @DCCore }
;  The name this copy pairs under. Per installation, not the literal
;  "dccore.mrc" (#649): the bot keeps one token per name and pairing a
;  name again replaces its token - so with every copy called the same,
;  pairing a laptop silently revoked the desktop, whose stored token
;  was then refused. The tail is the mIRC folder hashed, which is what
;  makes two installs two names; the same install pairing again still
;  replaces its own token, which is how a lost one is rotated.
alias dccore.client { return dccore.mrc- $+ $left($md5($mircdir),8) }
alias dccore.opt { return $hget(dccore,$1) }
alias dccore.st { return $hget(dccore.live,$1) }
alias dccore.nbsp { return $chr(160) }
alias dccore.dot { return $chr(183) }
; " in #channel" after a nick, or nothing when the bot sent "-" (a request by
; private message, or one whose channel is no longer known). Written to be
; joined to the nick with $+ so an empty answer leaves no stray space.
;
; The spaces are non-breaking ones, $chr(160). A real space at the start of what
; an alias returns is dropped by mIRC, so the nick and "in" ran together
; ("FLACin #channel"); a non-breaking space is not a space to it and stays.
alias dccore.in { if ($1 == $null) || ($1 == -) { return } | return $+($chr(160),in,$chr(160),$1) }

alias dccore.init {
  if (!$hget(dccore)) { hmake dccore 32 }
  if (!$hget(dccore.live)) { hmake dccore.live 64 }
  if ($isfile($dccore.ini)) { hload dccore $dccore.ini }
  ; defaults only where nothing is saved yet, so an upgrade keeps choices
  dccore.default auto 1
  dccore.default panel 1
  dccore.default titlebar 1
  dccore.default separate 0
  dccore.default beep 1
  dccore.default font 1
  dccore.default bg -1
  dccore.default statusmin 5
  dccore.default wantopen 0
  dccore.default show.request 1
  dccore.default show.queued 1
  dccore.default show.sends 1
  dccore.default show.fail 1
  dccore.default show.search 1
  dccore.default show.joins 0
  dccore.default show.bans 1
  dccore.default show.info 1
  dccore.default col.request 10
  dccore.default col.queued 7
  dccore.default col.sends 3
  dccore.default col.fail 4
  dccore.default col.search 12
  dccore.default col.joins 14
  dccore.default col.bans 5
  dccore.default col.info 14
  dccore.default col.console 6
  dccore.default col.name 2
  dccore.default col.head 2
}
alias dccore.default { if ($hget(dccore,$1) == $null) { hadd dccore $1 $2- } }
alias dccore.save { hsave -o dccore $dccore.ini }
alias dccore.set { hadd dccore $1 $2- | dccore.save }
alias dccore.forget { hdel dccore $1 | dccore.save }

on *:START: { dccore.init }
on *:LOAD: {
  dccore.init
  echo 14 -a DCCore window script $dccore.ver loaded. Type /dccore pair <botnick> to connect for the first time, /dccore for help.
}
on *:UNLOAD: {
  dccore.timers.off
  if ($hget(dccore)) { dccore.save | hfree dccore }
  if ($hget(dccore.live)) { hfree dccore.live }
}

; ---------------------------------------------------------------------
;  /dccore - the command
; ---------------------------------------------------------------------

alias dccore {
  dccore.init
  var %cmd = $1
  if (%cmd == connect) {
    if ($2 != $null) { dccore.set bot $2 }
    if ($dccore.bot == $null) { dccore.sys No bot nick yet. Use: /dccore connect <botnick> | return }
    dccore.remember.net
    dccore.set wantopen 1
    hadd dccore.live tries 0
    dccore.connect byhand
    return
  }
  if (%cmd == pair) {
    if ($2 != $null) { dccore.set bot $2 }
    if ($dccore.bot == $null) { dccore.sys No bot nick yet. Use: /dccore pair <botnick> | return }
    dccore.remember.net
    dccore.set wantopen 1
    hadd dccore.live pairing 1
    hadd dccore.live tries 0
    if ($dccore.st(state) == in) { dccore.send pair $dccore.client $dccore.ver | return }
    dccore.sys Pairing with $dccore.bot $+ : when the bot asks for the password, type it here once. The script keeps a token of its own from then on.
    dccore.connect byhand
    return
  }
  if (%cmd == unpair) {
    if ($dccore.st(state) == in) { dccore.send unpair $dccore.client | dccore.sys Token forgotten here and revoked on the bot. }
    else { dccore.sys Token forgotten here. To revoke it on the bot as well, type "unpair $dccore.client $+ " in the console once connected. }
    dccore.forget token
    dccore.forget paired
    dccore.forget bothost
    dccore.forget net
    hdel dccore.live tokenbad
    dccore.title
    return
  }
  if (%cmd == disconnect) {
    dccore.set wantopen 0
    dccore.timers.off
    if ($chat($dccore.bot)) { window -c $+(=,$dccore.bot) }
    dccore.sys Disconnected. Automatic reconnection is off until /dccore connect.
    return
  }
  if (%cmd == trust) {
    var %now = $address($dccore.bot,2)
    if (%now == $null) { dccore.sys $dccore.bot $+ 's host is not known: be in a channel with it first. | return }
    dccore.set bothost %now
    dccore.sys The token may now go to $dccore.bot at %now $+ . /dccore connect to try again.
    return
  }
  if (%cmd == options) { dccore.options | return }
  if (%cmd == lists) { dccore.send lists | return }
  if (%cmd == fetch) { dccore.send fetch $2- | return }
  if (%cmd == window) { dccore.window | window -a $dccore.win | return }
  if (%cmd == status) { dccore.send status | return }
  if (%cmd == raw) { dccore.send $2- | return }
  if (%cmd == panel) { dccore.set panel $iif($2 == off,0,1) | dccore.rebuild | return }
  if (%cmd == version) { dccore.sys dccore.mrc $dccore.ver $+ , protocol 1. $+ $dccore.protominor $+ , for the DCCore it ships with and later. Pairs as $dccore.client $+ . $iif($dccore.opt(net),Bot on $dccore.opt(net) $+ .,) | return }
  if (%cmd == font) {
    if ($2 !isnum) || ($2 < 6) { dccore.sys Give a size, like /dccore font 14 (now: $dccore.fontsize $+ ). | return }
    dccore.set fontsize $2
    dccore.set font 1
    if ($window($dccore.win)) { font $dccore.win $2 Lucida Console }
    dccore.sys Font size $2 $+ .
    return
  }
  echo 14 -a DCCore window script $dccore.ver - commands:
  echo 14 -a $dccore.nbsp $+ $dccore.nbsp /dccore pair <botnick> $+ $str($dccore.nbsp,7) first time: connect, log in once by hand, keep a token
  echo 14 -a $dccore.nbsp $+ $dccore.nbsp /dccore connect [botnick] $+ $str($dccore.nbsp,4) open the window and the chat (logs in with the token)
  echo 14 -a $dccore.nbsp $+ $dccore.nbsp /dccore disconnect $+ $str($dccore.nbsp,11) close the chat and stop reconnecting
  echo 14 -a $dccore.nbsp $+ $dccore.nbsp /dccore unpair $+ $str($dccore.nbsp,15) forget the token here and revoke it on the bot
  echo 14 -a $dccore.nbsp $+ $dccore.nbsp /dccore trust $+ $str($dccore.nbsp,16) accept the bot's current host as the one to send the token to
  echo 14 -a $dccore.nbsp $+ $dccore.nbsp /dccore options $+ $str($dccore.nbsp,14) what to show, colours, panel, title bar, beep
  echo 14 -a $dccore.nbsp $+ $dccore.nbsp /dccore window $+ $str($dccore.nbsp,15) open or focus @DCCore
  echo 14 -a $dccore.nbsp $+ $dccore.nbsp /dccore status $+ $str($dccore.nbsp,15) ask the bot for its status
  echo 14 -a $dccore.nbsp $+ $dccore.nbsp /dccore lists $+ $str($dccore.nbsp,16) the bots' lists we hold, and which have changed
  echo 14 -a $dccore.nbsp $+ $dccore.nbsp /dccore fetch [bot] $+ $str($dccore.nbsp,10) ask the bots whose lists changed, or one bot
  echo 14 -a $dccore.nbsp $+ $dccore.nbsp /dccore raw <command> $+ $str($dccore.nbsp,8) send any console command (or just type it in the window)
  echo 14 -a $dccore.nbsp $+ $dccore.nbsp /dccore panel on|off $+ $str($dccore.nbsp,8) the side panel
  echo 14 -a $dccore.nbsp $+ $dccore.nbsp /dccore font <size> $+ $str($dccore.nbsp,10) the window's font size (now $dccore.fontsize $+ )
  echo 14 -a Bot: $iif($dccore.bot,$dccore.bot,not set) $+ . Paired: $iif($dccore.opt(token),yes ( $+ $dccore.opt(paired) $+ ),no) $+ . Chat: $iif($dccore.st(state),$dccore.st(state),closed) $+ .
}

; ---------------------------------------------------------------------
;  Connection
; ---------------------------------------------------------------------

; "byhand" is the operator's own /dccore connect or pair; the retry timer,
; a JOIN of the bot's nick and an IRC connect dial without it.
;  Which network the bot lives on (#661). Nothing recorded it: the dial
;  ran in whatever connection fired it - on CONNECT/JOIN/401/CHATCLOSE
;  the event's own, on /dccore connect the active window's - so on a
;  client on two networks the CTCP went to the wrong one (401, a retry
;  loop stuck there) and every reconnect of the other network said
;  "already open". The network is kept from the moment the operator
;  typed /dccore connect or pair (that connection IS the bot's), or from
;  the bot's own JOIN, and every dial is moved onto it with /scid.
alias dccore.remember.net { if ($server) { dccore.set net $iif($network,$network,$server) } }
;  The connection id the bot's network is on right now, or $null when
;  that network is not connected. With nothing recorded: this one.
alias dccore.cid {
  var %net = $dccore.opt(net)
  if (%net == $null) { return $cid }
  var %i = 1
  while (%i <= $scon(0)) {
    if ($scon(%i).network == %net) || ($scon(%i).server == %net) { return $scon(%i).cid }
    inc %i
  }
  return
}
;  True when this connection is the bot's network, or none is recorded yet.
alias dccore.here { return $iif($dccore.opt(net) == $null,$true,$iif($network == $dccore.opt(net),$true,$iif($server == $dccore.opt(net),$true,$false))) }
alias dccore.connect {
  if ($dccore.bot == $null) { return }
  ; On the bot's network, not the one that happened to fire this (#661).
  var %cid = $dccore.cid
  if (%cid == $null) { dccore.sys Not connected to $dccore.opt(net) $+ , where $dccore.bot lives; the chat will open when you are. | return }
  if (%cid != $cid) { scid %cid dccore.connect $1- | return }
  if (!$server) { dccore.sys Not connected to IRC; the chat will open when you are. | return }
  if ($chat($dccore.bot)) { dccore.sys A chat with $dccore.bot is already open. | return }
  dccore.window
  hadd dccore.live state opening
  hadd dccore.live byhand $iif($1 == byhand,1,0)
  hadd dccore.live tokentried 0
  hadd dccore.live typed 0
  hadd dccore.live opened $ctime
  dccore.sys Opening the console of $dccore.bot $+ ...
  dccore.title
  dcc chat $dccore.bot
  ; mIRC never gives up on its own offer: when the bot does not answer
  ; the CTCP (it is offline without a 401, cannot reach this client, or
  ; refused this address without a word - it does after three wrong
  ; passwords) the =bot window sits at "Waiting for acknowledgement..."
  ; for ever, $chat() stays true and every later connect says "already
  ; open". This timer is the only way out of that state.
  .timerdccoreOpen 1 75 dccore.noanswer
}

; A chat that never comes up (bot offline, dial refused) is retried with
; backoff - 5 s, 15 s, 60 s, then every 2 minutes - and at once when the
; bot's nick joins a channel we share.
alias dccore.retry {
  if (!$dccore.opt(auto)) { return }
  if (!$dccore.opt(wantopen)) { return }
  if ($dccore.st(tokenbad)) { dccore.sys Not redialing by itself: the stored token was refused. /dccore pair again, or /dccore connect and type the password. | return }
  var %n = $calc($dccore.st(tries) + 1)
  hadd dccore.live tries %n
  var %delay = $gettok(5 15 60 120,$iif(%n > 4,4,%n),32)
  hadd dccore.live state waiting
  dccore.sys Trying again in $duration(%delay) $+ .
  dccore.title
  .timerdccoreRetry 1 %delay dccore.connect
}
; The offer was never answered: close the window mIRC left waiting and
; go through the same backoff as a chat the bot refused. Closing it may
; fire CHATCLOSE, which retries by itself; the state check keeps the two
; from both retrying.
alias dccore.noanswer {
  if ($dccore.st(state) != opening) { return }
  dccore.sys No answer from $dccore.bot in 75 seconds: it is offline, cannot reach this client, or refused this address.
  if ($chat($dccore.bot)) { window -c $+(=,$dccore.bot) }
  if ($dccore.st(state) == opening) { hadd dccore.live state closed | dccore.title | dccore.retry }
}
alias dccore.timers.off {
  .timerdccoreOpen off
  .timerdccoreRetry off
  .timerdccoreHB off
  .timerdccoreHello off
  .timerdccorePanel off
}

raw 401:*: {
  if ($2 == $dccore.bot) && ($dccore.st(state) == opening) {
    dccore.sys $dccore.bot is not online.
    if ($chat($dccore.bot)) { window -c $+(=,$dccore.bot) }
    dccore.retry
    haltdef
  }
}

on *:JOIN:#: {
  if ($nick == $dccore.bot) && ($dccore.here) && ($dccore.opt(wantopen)) && ($dccore.opt(auto)) && (!$chat($dccore.bot)) {
    if ($dccore.opt(net) == $null) { dccore.remember.net }
    hadd dccore.live tries 0
    .timerdccoreRetry 1 3 dccore.connect
  }
}

on *:CONNECT: {
  if ($dccore.here) && ($dccore.opt(wantopen)) && ($dccore.opt(auto)) && ($dccore.bot != $null) {
    hadd dccore.live tries 0
    .timerdccoreRetry 1 8 dccore.connect
  }
}

on *:CHATCLOSE: {
  if ($nick != $dccore.bot) { return }
  var %was = $dccore.st(state)
  hadd dccore.live state closed
  hdel dccore.live mode
  .timerdccoreOpen off
  .timerdccoreHB off
  .timerdccoreHello off
  dccore.sys Console closed $+ $iif(%was == in,$chr(32) $+ after $duration($calc($ctime - $dccore.st(opened)))) $+ .
  dccore.title
  dccore.panel
  if (%was == taken) {
    ; another client took the session; reconnecting now would only take
    ; it straight back and the two of you would trade it for ever
    dccore.sys Not reconnecting by itself: another client took over the console. /dccore connect takes it back.
    return
  }
  if (%was == in) { hadd dccore.live tries 0 }
  dccore.retry
}

; Every line the bot sends on the chat lands here. The chat window itself
; is hidden the moment the first line arrives; @DCCore is the window.
on ^*:CHAT:*: {
  if ($nick != $dccore.bot) { return }
  if (!$istok(in auth password taken,$dccore.st(state),32)) {
    hadd dccore.live state banner
    hadd dccore.live tries 0
    .timerdccoreOpen off
    if ($window($+(=,$nick))) { window -h $+(=,$nick) }
    dccore.title
  }
  ; the heartbeat is the STATUS burst, which only a structured session
  ; gets; a plain session can be quiet for an hour and be perfectly well
  if ($dccore.st(mode) == structured) { .timerdccoreHB 1 90 dccore.dead }
  dccore.line $1-
  haltdef
}

alias dccore.dead {
  dccore.sys Nothing heard from $dccore.bot for 90 seconds; the link looks dead. Reconnecting.
  if ($chat($dccore.bot)) { window -c $+(=,$dccore.bot) }
}

alias dccore.send {
  if (!$chat($dccore.bot)) { dccore.sys Not connected. /dccore connect | return }
  .msg $+(=,$dccore.bot) $1-
}

; ---------------------------------------------------------------------
;  Reading what the bot says
; ---------------------------------------------------------------------

alias dccore.line {
  var %state = $dccore.st(state)
  var %text = $strip($1-)
  if ($1 == DCCORE) { dccore.structured $2- | return }

  if (%text == Enter Your Password:) {
    if ($dccore.opt(token) != $null) && (!$dccore.st(tokentried)) && (!$dccore.st(tokenbad)) && (!$dccore.st(pairing)) {
      ; The token is only ever handed to the address the bot was paired from.
      ; Anyone on the network can take the bot's nick while it is away, and
      ; the script dials that nick by itself - so who answers is checked
      ; first, not assumed.
      if (!$dccore.peerok) { return }
      hadd dccore.live tokentried 1
      hadd dccore.live state auth
      dccore.send $dccore.opt(token)
      return
    }
    ; No token to send, so the operator would type the password - into a
    ; chat the script may have dialled by itself, to whoever holds the nick.
    ; Only a chat the operator opened (/dccore connect or pair) is their own
    ; act; one the script opened asks for the password only from the host the
    ; bot is known to have. (Nested: mIRC evaluates every identifier in an
    ; if-line, and peerok closes the chat when it says no.)
    if (!$dccore.st(byhand)) {
      if (!$dccore.peerok password) { return }
    }
    hadd dccore.live state password
    dccore.sys Type the admin password here and press Enter. $iif($dccore.st(pairing),The script will then ask the bot for a token of its own.,$iif($dccore.st(tokenbad),(The stored token was refused and is not sent again; /dccore pair replaces it.),(No token stored; /dccore pair keeps one.)))
    return
  }
  if (%text == Entering DCC Chat Admin Interface) {
    hadd dccore.live state in
    hdel dccore.live mode
    dccore.sys Logged in to $dccore.bot $+ . Saying hello...
    if ($dccore.st(tokenbad)) && (!$dccore.st(pairing)) { dccore.sys The stored token is still the one the bot refused: /dccore pair replaces it. }
    hdel dccore.live tokenbad
    dccore.send hello dccore.mrc $dccore.ver
    .timerdccoreHello 1 6 dccore.plain
    if ($dccore.st(pairing)) { dccore.send pair $dccore.client $dccore.ver }
    dccore.title
    return
  }
  if (%text == Incorrect Password.) {
    ; state auth with nothing typed by hand: it was the stored token
    if (%state == auth) && (!$dccore.st(typed)) {
      ; The bot counts refusals per address across sessions and blocks the
      ; address for 15 minutes at the third. Sent again on every automatic
      ; redial, a revoked token would reach that on its own within minutes,
      ; with the operator away; so it is not sent again and the redial waits
      ; for the operator (a login, or a new token) instead.
      hadd dccore.live tokenbad 1
      dccore.sys The bot refused the stored token (revoked there, or replaced by a newer pairing). It is not sent again, and the script does not redial by itself: type the password now, or /dccore pair again. (Three refusals block this address for 15 minutes.)
      hadd dccore.live state password
      return
    }
    dccore.sys Wrong password. (Three wrong ones close the chat and block your address for 15 minutes.)
    return
  }
  if (%text == For help type "help") { return }
  if (Session taken over from * iswm %text) {
    dccore.sys $dccore.bot $+ : %text
    hadd dccore.live state taken
    return
  }
  if (Unknown command: hello* iswm %text) { dccore.plain | return }

  ; plain mode, or the banner before login: the line as it is, with
  ; whatever colours the bot's own theme put on it
  dccore.echo $1-
}

; A bot without the structured feed (from before `hello`), or one whose
; protocol we do not know: the window shows the chat as it comes.
; Is the one on the other end of the chat the bot we paired with? The host
; the bot had when the token was stored is kept (bothost) and compared with
; the host the nick has NOW. A script paired before this check has none
; stored and learns it the first time the bot's host is known; if it is not
; known yet, the token waits rather than goes out unchecked.
; $1 is what is being withheld: "password" (the prompt in a chat the script
; dialled by itself), else the token.
alias dccore.peerok {
  var %what = $iif($1 == password,asking for the password,sending the token)
  var %now = $address($dccore.bot,2)
  var %known = $dccore.opt(bothost)
  if (%known == $null) {
    if (%now == $null) {
      dccore.sys Not %what yet: $dccore.bot $+ 's host is not known, so it cannot be checked that this is the bot. Join a channel it is in, or /dccore trust once you are sure, then /dccore connect.
      dccore.abandon
      return $false
    }
    dccore.set bothost %now
    dccore.sys Remembering that $dccore.bot is at %now $+ ; the script will only log in there.
    return $true
  }
  if (%now == %known) { return $true }
  dccore.sys NOT %what $+ : $dccore.bot is at $iif(%now,%now,an unknown host) $+ , but the bot is known to be at %known $+ . Someone else may hold its nick. If the bot really moved, /dccore trust, then /dccore connect.
  dccore.abandon
  return $false
}
; give up on this chat and do not retry by itself until told to
alias dccore.abandon {
  dccore.set wantopen 0
  dccore.timers.off
  hadd dccore.live state closed
  if ($chat($dccore.bot)) { window -c $+(=,$dccore.bot) }
  dccore.title
}
alias dccore.plain {
  .timerdccoreHello off
  if ($dccore.st(state) != in) { return }
  if ($dccore.st(mode) == structured) { return }
  if ($dccore.st(mode) == plain) { return }
  hadd dccore.live mode plain
  dccore.sys Plain mode: this bot does not send the structured feed, so there is no panel; the chat is shown as it comes. Console commands still work.
  dccore.title
  dccore.panel
}

; ---------------------------------------------------------------------
;  The structured feed: DCCORE <TYPE> <fixed fields...> <free text>
; ---------------------------------------------------------------------

alias dccore.structured {
  var %type = $1
  if (%type == HELLO) {
    .timerdccoreHello off
    ; $2 is major.minor (a bot from before the minor was added says a bare 1).
    ; A major we do not know: plain mode. A minor we do not know: the
    ; lines still parse, but a field was inserted on one side, so say so.
    var %major = $gettok($2,1,46)
    var %minor = $gettok($2,2,46)
    if (%minor == $null) { var %minor = 0 }
    if (%major != 1) {
      dccore.sys $dccore.bot speaks protocol $2 and this script knows 1. $+ $dccore.protominor $+ : falling back to plain mode. Update the script.
      dccore.plain
      return
    }
    if (%minor != $dccore.protominor) {
      dccore.sys $dccore.bot speaks feed 1. $+ %minor and this script was written for 1. $+ $dccore.protominor $+ : some lines will show fields in the wrong place. Update whichever is older - for the script, save the new dccore.mrc over the old one and /reload -rs dccore.mrc
    }
    hadd dccore.live mode structured
    .timerdccoreHB 1 90 dccore.dead
    hadd dccore.live failed 0
    hadd dccore.live searches 0
    hadd dccore.live lastecho 0
    dccore.sys --- connected to the console of $3 (DCCore $4- $+ , $iif($dccore.opt(token),paired client,not paired) $+ ) ---
    return
  }
  if (%type == STATUS) { dccore.status $2- | return }
  ; PING: the bot could not read its figures in time and sent this so the
  ; link is heard from; the heartbeat timer above was reset by it already
  if (%type == PING) { return }
  if (%type == SLOT) { hadd dccore.live slot. $+ $dccore.st(nslots) $2- | hinc dccore.live nslots | dccore.panel.soon | return }
  if (%type == QUEUE) { hadd dccore.live queue. $+ $2 $3- | dccore.panel.soon | return }
  if (%type == OUT) { dccore.out $2- | return }
  if (%type == LISTFETCH) {
    ; <bot> <auto|arrived|unusable> <text>: a held bot list asked for again,
    ; arrived, or not usable. The text already names the bot.
    dccore.echo $dccore.tag(LISTS,search) $4-
    return
  }
  if (%type == TAKEN) {
    dccore.sys $dccore.bot $+ : another client ( $+ $2 $+ ) took over the console.
    hadd dccore.live state taken
    return
  }
  if (%type == DROPPED) {
    dccore.echo $dccore.tag(DROPPED,fail) $2 line(s) were dropped by the bot: this client fell behind.
    return
  }
  if (%type == TOKEN) {
    dccore.set token $3
    dccore.set paired $date
    hdel dccore.live tokenbad
    if ($address($dccore.bot,2) != $null) { dccore.set bothost $address($dccore.bot,2) }
    hadd dccore.live pairing 0
    dccore.sys Paired as $2 $+ . The token is kept in dccore.ini beside the script, in clear text - keep that file as you would a password; from now on the script logs in by itself. /dccore unpair revokes it.
    dccore.title
    return
  }
  if (%type == REQUEST) {
    if (!$dccore.opt(show.request)) { return }
    dccore.echo $dccore.tag(REQUEST,request) $2 $+ $dccore.in($3) asked for $iif($4 == folder,the folder) $dccore.name($5-)
    return
  }
  if (%type == QUEUED) {
    if (!$dccore.opt(show.queued)) { return }
    dccore.echo $dccore.tag(QUEUED,queued) $dccore.name($7-) for $2 $+ $dccore.in($3) at # $+ $4 ( $+ $5 $+ / $+ $6 slots busy)
    return
  }
  if (%type == SENDING) {
    if (!$dccore.opt(show.sends)) { return }
    dccore.echo $dccore.tag(SENDING,sends) $dccore.name($7-) to $2 $+ $dccore.in($3) (slot $4 $+ / $+ $5 $+ , $dccore.bytes($6) $+ )
    return
  }
  if (%type == RESUMED) {
    if (!$dccore.opt(show.sends)) { return }
    dccore.echo $dccore.tag(RESUMED,sends) $dccore.name($6-) for $2 $+ $dccore.in($3) at $dccore.bytes($4) of $dccore.bytes($5)
    return
  }
  if (%type == SENT) {
    if (!$dccore.opt(show.sends)) { return }
    dccore.echo $dccore.tag(SENT,sends) $dccore.name($7-) to $2 $+ $dccore.in($3) $+ : $dccore.bytes($4) in $dccore.dur($5) $iif($6 > 0,at $dccore.speed($6),at n/a)
    return
  }
  if (%type == FAIL) {
    hinc dccore.live failed
    if (!$dccore.opt(show.fail)) { return }
    var %rest = $6-
    var %name = %rest
    var %why = failed
    var %p = $pos(%rest,$+($chr(32),::,$chr(32)),1)
    if (%p) {
      %name = $left(%rest,$calc(%p - 1))
      %why = $mid(%rest,$calc(%p + 4))
    }
    dccore.echo $dccore.tag(FAILED,fail) $dccore.name(%name) to $2 $+ $dccore.in($3) - %why ( $+ $dccore.bytes($4) of $dccore.bytes($5) arrived)
    if ($dccore.opt(beep)) { beep 2 200 }
    return
  }
  if (%type == SEARCH) {
    hinc dccore.live searches
    if (!$dccore.opt(show.search)) { return }
    dccore.echo $dccore.tag(SEARCH,search) $2 $+ $dccore.in($3) searched $dccore.name($5-) -> $4 result(s)
    return
  }
  if (%type == LOG) {
    var %cat = $2
    var %group = info
    if ($istok(JOIN PART QUIT,%cat,32)) { %group = joins }
    elseif ($istok(BAN HARDBAN MUTE TBAN,%cat,32)) { %group = bans }
    if (!$dccore.opt(show. $+ %group)) { return }
    dccore.echo $dccore.tag(%cat,%group) $3-
    return
  }
  ; a type this script does not know: a newer bot, same major - show it
  dccore.echo $dccore.tag(%type,info) $2-
}

; STATUS <used> <slots> <qfiles> <qusers> <sent_today> <bytes_today> <bps_now> <record_bps>
alias dccore.status {
  hadd dccore.live st.used $1
  hadd dccore.live st.slots $2
  hadd dccore.live st.qfiles $3
  hadd dccore.live st.qusers $4
  hadd dccore.live st.sent $5
  hadd dccore.live st.bytes $6
  hadd dccore.live st.bps $7
  hadd dccore.live st.record $8
  ; since the BOT started (#754); an older bot sends only eight fields and the
  ; panel falls back to the window's own figures
  if ($9 isnum) { hadd dccore.live st.started $9 }
  if ($10 isnum) { hadd dccore.live st.failed $10 }
  if ($11 isnum) { hadd dccore.live st.searches $11 }
  ; the SLOT and QUEUE lines of this burst follow at once; start afresh
  hdel -w dccore.live slot.*
  hdel -w dccore.live queue.*
  hadd dccore.live nslots 1
  dccore.title
  dccore.panel.soon
  if ($dccore.opt(statusmin) > 0) && ($calc($ctime - $dccore.st(lastecho)) >= $calc($dccore.opt(statusmin) * 60)) {
    hadd dccore.live lastecho $ctime
    dccore.echo $dccore.tag(STATUS,info) slots $1 $+ / $+ $2 $dccore.dot queue $4 ( $+ $3 files) $dccore.dot today $5 files / $dccore.bytes($6) $dccore.dot $dccore.speed($7) now, record $dccore.speed($8)
  }
}

; ---------------------------------------------------------------------
;  Drawing: the text, the title bar, the side panel
; ---------------------------------------------------------------------

alias dccore.window {
  if ($window($dccore.win)) { return }
  if ($dccore.opt(panel)) { window -el30 $dccore.win }
  else { window -e $dccore.win }
  if ($dccore.opt(font)) { font $dccore.win $dccore.fontsize Lucida Console }
  dccore.background
  dccore.title
  dccore.panel
}

; The window's background colour. mIRC has no per-window colour setting
; (/color background is for every window at once), only a per-window
; PICTURE, so the colour is a .bmp beside the script, tiled. -1 is "leave it
; as mIRC has it": the picture is removed. Written on first use and kept, one
; file per colour.
;
; 128x128, not one pixel. Tiled, a one-pixel picture is drawn one pixel at a
; time - over the whole window, on every repaint - and a window with a long
; history crawled: every new line and every change to the options froze it.
; 128x128 is a few hundred draws for the same window, and 48 KB on disk.
alias dccore.background {
  if (!$window($dccore.win)) { return }
  var %c = $dccore.opt(bg)
  if (%c !isnum) || (%c < 0) || (%c > 15) { background -x $dccore.win | return }
  var %f = $dccore.bgfile(%c)
  if (%f) { background -t $dccore.win $qt(%f) }
}
alias dccore.bgfile {
  var %f = $+($scriptdir,dccore-bg-,$1,-128.bmp)
  if ($isfile(%f)) { return %f }
  var %rgb = $dccore.rgb($1)
  ; The 54-byte header of a 128x128, 24-bit bitmap: 49152 bytes of pixels,
  ; 49206 in all, 384 bytes (already a multiple of four) to a row.
  bset &dccorebg 1 66 77 54 192 0 0 0 0 0 0 54 0 0 0 40 0 0 0 128 0 0 0 128 0 0 0 1 0 24 0 0 0 0 0 0 192 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0
  ; One row is 128 pixels of blue, green, red; the rows are all the same.
  var %px = $gettok(%rgb,3,46) $gettok(%rgb,2,46) $gettok(%rgb,1,46)
  var %row = $str(%px $chr(32),128)
  var %y = 0
  while (%y < 128) { bset &dccorebg $calc(55 + %y * 384) %row | inc %y }
  bwrite $qt(%f) 0 -1 &dccorebg
  bunset &dccorebg
  if ($isfile(%f)) { return %f }
}
; mIRC's default palette, by colour number, as red.green.blue
alias dccore.rgb { return $gettok(255.255.255 0.0.0 0.0.127 0.147.0 255.0.0 127.0.0 156.0.156 252.127.0 255.255.0 0.252.0 0.147.147 0.255.255 0.0.252 255.0.255 127.127.127 210.210.210,$calc($1 + 1),32) }

; The font size: what the operator set with /dccore font <size> (or in the
; options), else the Status window's, else 12. A fixed 9pt was unreadable
; on a high-resolution screen on the first real run, and what a window
; "should" be is the operator's to say.
alias dccore.fontsize {
  var %size = $dccore.opt(fontsize)
  if (%size isnum) && (%size >= 6) { return %size }
  %size = $window(Status Window).fontsize
  if (%size isnum) && (%size >= 6) { return %size }
  return 12
}

; The panel is a listbox the window is created with or without, so a
; change means a new window: the text is carried over line by line.
alias dccore.rebuild {
  if (!$window($dccore.win)) { dccore.window | return }
  var %n = $line($dccore.win,0), %i = 1
  ; The text is copied out BEFORE the window is closed - a closed window has
  ; no lines to read - into a table, and an empty line is kept as a
  ; non-breaking space (/hadd and /echo both refuse an empty text, and either
  ; error would halt this alias half way).
  if ($hget(dccore.rb)) { hfree dccore.rb }
  hmake dccore.rb 100
  while (%i <= %n) {
    var %t = $line($dccore.win,%i)
    if (%t == $null) { var %t = $dccore.nbsp }
    hadd dccore.rb %i %t
    inc %i
  }
  ; The time the rebuild began, not 1: the CLOSE handler ignores a close
  ; while one is under way, and a rebuild that stopped half way must not
  ; leave the window impossible to close for good.
  hadd dccore.live rebuilding $ticks
  window -c $dccore.win
  dccore.window
  var %i = 1
  while (%i <= %n) { echo -i2 $dccore.win $hget(dccore.rb,%i) | inc %i }
  hfree dccore.rb
  hadd dccore.live rebuilding 0
}

; the tag at the start of each line: bold, in the group's colour,
; padded so the text after it lines up in a fixed-width font
alias dccore.tag {
  var %t = $+($chr(91),$1,$chr(93))
  return $+($chr(2),$chr(3),$dccore.col($2),%t,$chr(15),$str($dccore.nbsp,$calc(10 - $len(%t))))
}
alias dccore.col { return $base($dccore.opt(col. $+ $1),10,10,2) }
alias dccore.name { return $+($chr(3),$dccore.col(name),",$1-,",$chr(15)) }
; Bytes as people read them - 27.5MB, 1.06MB/s - formatted here rather
; than by $bytes().suffix, which on the first real run gave "27.5" and
; "1.06/s" with no unit at all. Two decimals up to 10, one up to 100,
; none above, so a column stays a column.
alias dccore.bytes {
  var %n = $1
  if (%n !isnum) { %n = 0 }
  if (%n >= 1073741824) { return $+($dccore.round($calc(%n / 1073741824)),GB) }
  if (%n >= 1048576) { return $+($dccore.round($calc(%n / 1048576)),MB) }
  if (%n >= 1024) { return $+($dccore.round($calc(%n / 1024)),KB) }
  return $+($int(%n),B)
}
alias dccore.round {
  if ($1 < 10) { return $round($1,2) }
  if ($1 < 100) { return $round($1,1) }
  return $round($1,0)
}
alias dccore.speed { return $+($dccore.bytes($1),/s) }
alias dccore.pad2 { return $iif($1 < 10,$+(0,$1),$1) }
alias dccore.dur {
  var %s = $int($1)
  if (%s >= 3600) { return $+($int($calc(%s / 3600)),:,$dccore.pad2($int($calc((%s % 3600) / 60))),:,$dccore.pad2($calc(%s % 60))) }
  return $+($int($calc(%s / 60)),:,$dccore.pad2($calc(%s % 60)))
}
; pad or cut to N characters, left- and right-aligned
alias dccore.fit { return $left($+($1,$str($dccore.nbsp,$2)),$2) }
alias dccore.rfit { return $right($+($str($dccore.nbsp,$2),$1),$2) }

; /echo refuses an empty text, and the bot's banner and `help` both have
; blank lines - so an empty line is drawn as a non-breaking space.
alias dccore.echo {
  dccore.window
  echo -ti2 $dccore.win $iif($1- == $null,$dccore.nbsp,$1-)
}
; the script's own remarks, in grey
alias dccore.sys {
  dccore.window
  echo 14 -ti2 $dccore.win $iif($1- == $null,$dccore.nbsp,$1-)
}
; a console command's reply
alias dccore.out {
  if ($dccore.opt(separate)) {
    if (!$window(@DCCore-console)) { window -e @DCCore-console }
    echo -ti2 @DCCore-console $iif($1- == $null,$dccore.nbsp,$1-)
    return
  }
  dccore.echo $dccore.tag(CONSOLE,console) $1-
}

alias dccore.title {
  if (!$window($dccore.win)) { return }
  var %bot = $iif($dccore.bot,$dccore.bot,DCCore)
  var %net = $iif($network,$network,$server)
  if ($dccore.st(state) != in) { titlebar $dccore.win %bot $dccore.dot $iif($dccore.st(state),$dccore.st(state),not connected) | return }
  if (!$dccore.opt(titlebar)) || ($dccore.st(mode) != structured) { titlebar $dccore.win %bot on %net | return }
  titlebar $dccore.win %bot on %net $dccore.dot slots $dccore.st(st.used) $+ / $+ $dccore.st(st.slots) $dccore.dot queue $dccore.st(st.qusers) $dccore.dot today $dccore.st(st.sent) files / $dccore.bytes($dccore.st(st.bytes)) $dccore.dot $dccore.speed($dccore.st(st.bps))
}

; SLOT and QUEUE lines arrive one by one after STATUS with no end marker,
; so the panel is redrawn a quarter of a second after the last of them.
alias dccore.panel.soon { .timerdccorePanel -m 1 250 dccore.panel }

alias dccore.panel {
  if (!$window($dccore.win)) { return }
  if (!$dccore.opt(panel)) { return }
  clear -l $dccore.win
  if ($dccore.st(mode) != structured) {
    aline -l 14 $dccore.win $iif($dccore.st(state) == in,(no panel in plain mode),(not connected))
    return
  }
  var %head = $dccore.opt(col.head)
  var %used = $dccore.st(st.used), %slots = $dccore.st(st.slots)
  aline -l %head $dccore.win Sending %used $+ / $+ %slots
  var %i = 1
  while ($dccore.st(slot. $+ %i) != $null) {
    var %l = $dccore.st(slot. $+ %i)
    ; <nick> <sent> <total> <bps> <name>
    var %pct = $iif($gettok(%l,3,32) > 0,$int($calc($gettok(%l,2,32) * 100 / $gettok(%l,3,32))),0)
    aline -l $dccore.opt(col.sends) $dccore.win > $dccore.fit($gettok(%l,1,32),9) $dccore.rfit($dccore.bytes($gettok(%l,3,32)),7) $dccore.rfit(%pct $+ $chr(37),4) $dccore.rfit($dccore.speed($gettok(%l,4,32)),9)
    inc %i
  }
  if (%used < %slots) { aline -l 14 $dccore.win $dccore.nbsp $+ $dccore.nbsp ( $+ $calc(%slots - %used) free) }
  aline -l 14 $dccore.win $dccore.nbsp
  aline -l %head $dccore.win Queue $dccore.st(st.qusers) $iif($dccore.st(st.qfiles) > 0,( $+ $dccore.st(st.qfiles) files))
  var %i = 1
  while ($dccore.st(queue. $+ %i) != $null) {
    var %l = $dccore.st(queue. $+ %i)
    ; <nick> <files> <frozen_secs_left>
    var %files = $gettok(%l,2,32)
    if ($gettok(%l,3,32) > 0) { aline -l $dccore.opt(col.fail) $dccore.win $dccore.rfit(%i,2) $dccore.fit($gettok(%l,1,32),9) frozen $dccore.dur($gettok(%l,3,32)) }
    else { aline -l $dccore.win $dccore.rfit(%i,2) $dccore.fit($gettok(%l,1,32),9) %files $iif(%files == 1,file,files) }
    inc %i
  }
  if ($dccore.st(st.qusers) > $calc(%i - 1)) { aline -l 14 $dccore.win $dccore.nbsp $+ $dccore.nbsp ... $calc($dccore.st(st.qusers) - %i + 1) more }
  if ($dccore.st(st.qusers) == 0) { aline -l 14 $dccore.win $dccore.nbsp $+ $dccore.nbsp (empty) }
  aline -l 14 $dccore.win $dccore.nbsp
  aline -l %head $dccore.win Today
  aline -l $dccore.win $dccore.nbsp sent $dccore.rfit($dccore.st(st.sent),6) / $dccore.bytes($dccore.st(st.bytes))
  aline -l $dccore.win $dccore.nbsp record $dccore.speed($dccore.st(st.record))
  aline -l 14 $dccore.win $dccore.nbsp
  var %since = $iif($dccore.st(st.started) > 0,$dccore.st(st.started),$dccore.st(opened))
  var %fmt = $iif($calc($ctime - %since) > 72000,ddd HH:nn,HH:nn)
  var %failed = $iif($dccore.st(st.failed) != $null,$dccore.st(st.failed),$dccore.st(failed))
  var %searches = $iif($dccore.st(st.searches) != $null,$dccore.st(st.searches),$dccore.st(searches))
  aline -l %head $dccore.win Since $asctime(%since,%fmt)
  aline -l $dccore.win $dccore.nbsp failed $dccore.rfit(%failed,4)
  aline -l $dccore.win $dccore.nbsp searches $dccore.rfit(%searches,2)
}

; the nick on the selected panel line, for the right-click menu.
; The panel shows a nick cut or padded to nine characters, so it is never read
; back from the text as a nick: a queue row starts with its number, which names
; the row in the status the panel was drawn from, and a sending row's nine
; characters are matched against the slots. Either way the whole nick comes
; back, with no padding on it.
;
; Plain string functions and no $regex: on a real mIRC the pattern that took
; the nine characters out of a sending row matched, and gave back an empty
; group. $sline, $mid, $remove and $gettok were seen to give what the panel
; text says (the row was 34 characters, ">", a space, then the nick).
alias dccore.selq {
  var %t = $sline($dccore.win,1)
  var %n = $remove($gettok(%t,1,32),$chr(160))
  if (%n isnum) && ($dccore.st(queue. $+ %n) != $null) { return $gettok($dccore.st(queue. $+ %n),1,32) }
}
alias dccore.sels {
  var %t = $sline($dccore.win,1)
  if ($asc($left(%t,1)) != 62) { return }
  var %f = $remove($mid(%t,3,9),$chr(160)), %i = 1
  if (%f == $null) { return }
  while ($dccore.st(slot. $+ %i) != $null) {
    var %n = $gettok($dccore.st(slot. $+ %i),1,32)
    if ($left(%n,9) == %f) { return %n }
    inc %i
  }
}

; ---------------------------------------------------------------------
;  What you type in the window goes to the bot
; ---------------------------------------------------------------------

on *:INPUT:@DCCore: {
  if ($left($1,1) == /) && ($left($1,2) != //) { return }
  if (!$chat($dccore.bot)) {
    if ($1 == connect) { dccore connect | halt }
    dccore.sys Not connected. /dccore connect (or /dccore pair the first time).
    halt
  }
  if ($dccore.st(state) == password) {
    ; the password, typed by hand: never shown, never stored
    hadd dccore.live state auth
    hadd dccore.live typed 1
    dccore.send $1-
    dccore.echo $dccore.prompt ********
    halt
  }
  dccore.echo $dccore.prompt $1-
  dccore.send $1-
  halt
}
on *:INPUT:@DCCore-console: {
  if ($left($1,1) == /) && ($left($1,2) != //) { return }
  echo -ti2 @DCCore-console $dccore.prompt $1-
  dccore.send $1-
  halt
}
alias dccore.prompt { return $+($chr(2),$chr(3),$dccore.col(console),>,$chr(15)) }

on *:CLOSE:@DCCore: {
  if ($dccore.st(rebuilding)) && ($calc($ticks - $dccore.st(rebuilding)) < 5000) { return }
  ; closing the window closes the chat too; /dccore connect reopens both
  dccore.set wantopen 0
  dccore.timers.off
  if ($chat($dccore.bot)) { window -c $+(=,$dccore.bot) }
}

; ---------------------------------------------------------------------
;  Right-click menus
; ---------------------------------------------------------------------

; Asking, for the menu items that need a word from you. $input is mIRC 6.0+.
; "eo" is an edit box with OK/Cancel; Cancel or an empty box sends nothing.
; No commas in the prompt texts: they would end the $input argument.
alias dccore.ask {
  var %v = $input($2-,eo,DCCore)
  if (%v != $null) { dccore.send $1 %v }
}
; The same for a console command typed in full.
alias dccore.askraw {
  var %v = $input(Console command (see help),eo,DCCore)
  if (%v != $null) { dccore.send %v }
}
; Yes or no first, for the ones that change something or take a while.
alias dccore.confirm {
  if ($input($2-,yq,DCCore)) { dccore.send $1 }
}
alias dccore.askfont {
  var %v = $input(Font size (6 or more),eo,DCCore)
  if (%v isnum) { dccore font %v }
}

; Every /dccore command, and every console command worth a click, is here.
menu @DCCore {
  Status:dccore.send status
  Slots:dccore.send slots
  Queue:dccore.send queue
  Bans:dccore.send bans
  Uptime:dccore.send uptime
  Version:dccore.send version
  -
  $iif($dccore.selq,Queue of $dccore.selq):dccore.send queue $dccore.selq
  $iif($dccore.selq,Clear the queue of $dccore.selq):dccore.send clearqueue $dccore.selq
  $iif($dccore.sels,Queue of $dccore.sels):dccore.send queue $dccore.sels
  -
  Lists
  .Show the lists:dccore lists
  .Fetch the changed lists:dccore fetch
  .Ask a bot for its list...:dccore.ask fetch Ask which bot for its list
  Library
  .Find duplicate filenames:dccore.send verify
  .Rebuild the list...:dccore.confirm update Rebuild the list? It walks the whole library and can take minutes.
  Admin
  .Ban...:dccore.ask ban Ban pattern (for example *!*@host.example)
  .Unban...:dccore.ask unban Pattern to remove
  .Clear a queue...:dccore.ask clearqueue Clear the queue of which nick
  .Reload the bot (rehash)...:dccore.confirm rehash Reload the bot's code and settings?
  Console command...:dccore.askraw
  -
  Connection
  .$iif($chat($dccore.bot),Disconnect,Connect):dccore $iif($chat($dccore.bot),disconnect,connect)
  .Pair with the bot:dccore pair $dccore.bot
  .Forget the token (unpair):dccore unpair
  .Trust the bot's host:dccore trust
  Window
  .Options...:dccore.options
  .Panel $iif($dccore.opt(panel),off,on):dccore panel $iif($dccore.opt(panel),off,on)
  .Font size...:dccore.askfont
  .Clear window:clear @DCCore
  Command list:dccore
}

menu nicklist {
  DCCore
  .Queue of $1:dccore.send queue $1
  .Clear the queue of $1:dccore.send clearqueue $1
}

menu status,channel {
  DCCore
  .Open the window:dccore window
  .Show the lists:dccore lists
  .Fetch the changed lists:dccore fetch
  .Command list:dccore
  .$iif($chat($dccore.bot),Disconnect,Connect):dccore $iif($chat($dccore.bot),disconnect,connect)
  .Options...:dccore.options
}

; ---------------------------------------------------------------------
;  Options dialog
; ---------------------------------------------------------------------

alias dccore.options {
  dccore.init
  if ($dialog(dccore.opt)) { dialog -v dccore.opt | return }
  dialog -m dccore.opt dccore.opt
}

dialog dccore.opt {
  title "DCCore window - options"
  size -1 -1 322 236
  option dbu
  box "Show in @DCCore", 100, 5 3 312 102
  check "Requests (who asked for what)", 101, 10 13 170 10
  check "Queue positions", 102, 10 24 170 10
  check "Sends: starting, resuming, done", 103, 10 35 170 10
  check "Failed transfers", 104, 10 46 170 10
  check "Searches and result counts", 105, 10 57 170 10
  check "Joins, parts, quits of queued users", 106, 10 68 170 10
  check "Bans, mutes, floods", 107, 10 79 170 10
  check "Other log lines", 108, 10 90 170 10
  combo 201, 192 12 52 70, drop
  combo 202, 192 23 52 70, drop
  combo 203, 192 34 52 70, drop
  combo 204, 192 45 52 70, drop
  combo 205, 192 56 52 70, drop
  combo 206, 192 67 52 70, drop
  combo 207, 192 78 52 70, drop
  combo 208, 192 89 52 70, drop
  text "File names", 210, 248 14 66 8
  combo 211, 248 23 52 70, drop
  text "Console replies", 212, 248 36 66 8
  combo 213, 248 45 52 70, drop
  text "Status every", 214, 248 60 66 8
  edit "", 215, 248 69 18 11, autohs
  text "min, 0 = off", 216, 268 71 49 8
  text "Panel headings", 217, 248 83 66 8
  combo 218, 248 91 52 70, drop
  box "Window", 300, 5 108 312 50
  check "Side panel: slots, queue and totals", 301, 10 118 170 10
  check "Slots, queue and speed in the title bar", 302, 10 129 170 10
  check "Console replies in a separate window", 303, 10 140 170 10
  check "Beep on a failed transfer", 304, 192 118 118 10
  check "Fixed-width font, size", 305, 192 129 100 10
  edit "", 306, 294 128 18 11, autohs
  text "Background", 307, 192 142 48 8
  combo 308, 242 140 56 70, drop
  box "Connection", 400, 5 161 312 52
  text "Bot nick", 401, 10 173 36 8
  edit "", 402, 48 171 56 11, autohs
  text "", 403, 110 173 204 8
  check "Reconnect and log in by itself when the bot comes back", 404, 10 186 300 10
  text "The bot's own Settings > Console feed is the ceiling on what is sent at all.", 405, 10 198 300 8
  button "OK", 1, 232 218 40 12, ok default
  button "Cancel", 2, 276 218 40 12, cancel
  button "Pair again...", 501, 5 218 46 12
  button "Forget token", 502, 54 218 46 12
}

alias dccore.colours { return 00 white,01 black,02 navy,03 green,04 red,05 maroon,06 purple,07 orange,08 yellow,09 lime,10 teal,11 cyan,12 blue,13 pink,14 grey,15 silver }
alias dccore.groups { return request queued sends fail search joins bans info }

on *:dialog:dccore.opt:init:0: {
  var %i = 1
  while (%i <= 8) {
    var %g = $gettok($dccore.groups,%i,32)
    if ($dccore.opt(show. $+ %g)) { did -c dccore.opt $calc(100 + %i) }
    dccore.fillcombo $calc(200 + %i) $dccore.opt(col. $+ %g)
    inc %i
  }
  dccore.fillcombo 211 $dccore.opt(col.name)
  dccore.fillcombo 213 $dccore.opt(col.console)
  dccore.fillcombo 218 $dccore.opt(col.head)
  did -ra dccore.opt 215 $dccore.opt(statusmin)
  if ($dccore.opt(panel)) { did -c dccore.opt 301 }
  if ($dccore.opt(titlebar)) { did -c dccore.opt 302 }
  if ($dccore.opt(separate)) { did -c dccore.opt 303 }
  if ($dccore.opt(beep)) { did -c dccore.opt 304 }
  if ($dccore.opt(font)) { did -c dccore.opt 305 }
  did -ra dccore.opt 306 $dccore.fontsize
  dccore.fillbg
  if ($dccore.bot) { did -ra dccore.opt 402 $dccore.bot }
  did -ra dccore.opt 403 $iif($dccore.opt(token),Paired $dccore.opt(paired) (token in dccore.ini),Not paired: the bot will ask for the password)
  if ($dccore.opt(auto)) { did -c dccore.opt 404 }
}
; "none" first, then the sixteen colours: the selected line is the colour + 2
alias dccore.fillbg {
  var %i = 1
  did -a dccore.opt 308 none (mIRC's)
  while (%i <= 16) { did -a dccore.opt 308 $gettok($dccore.colours,%i,44) | inc %i }
  did -c dccore.opt 308 $calc($dccore.opt(bg) + 2)
}
alias dccore.fillcombo {
  var %i = 1
  while (%i <= 16) { did -a dccore.opt $1 $gettok($dccore.colours,%i,44) | inc %i }
  did -c dccore.opt $1 $calc($2 + 1)
}
on *:dialog:dccore.opt:sclick:1: {
  var %i = 1
  var %panel = $dccore.opt(panel)
  var %bg = $dccore.opt(bg)
  while (%i <= 8) {
    var %g = $gettok($dccore.groups,%i,32)
    hadd dccore show. $+ %g $did(dccore.opt,$calc(100 + %i)).state
    hadd dccore col. $+ %g $calc($did(dccore.opt,$calc(200 + %i)).sel - 1)
    inc %i
  }
  hadd dccore col.name $calc($did(dccore.opt,211).sel - 1)
  hadd dccore col.console $calc($did(dccore.opt,213).sel - 1)
  hadd dccore col.head $calc($did(dccore.opt,218).sel - 1)
  hadd dccore statusmin $iif($did(dccore.opt,215).text isnum,$int($did(dccore.opt,215).text),5)
  hadd dccore panel $did(dccore.opt,301).state
  hadd dccore titlebar $did(dccore.opt,302).state
  hadd dccore separate $did(dccore.opt,303).state
  hadd dccore beep $did(dccore.opt,304).state
  hadd dccore font $did(dccore.opt,305).state
  if ($did(dccore.opt,306).text isnum) && ($did(dccore.opt,306).text >= 6) { hadd dccore fontsize $did(dccore.opt,306).text }
  hadd dccore bg $calc($did(dccore.opt,308).sel - 2)
  hadd dccore auto $did(dccore.opt,404).state
  if ($did(dccore.opt,402).text != $null) { hadd dccore bot $did(dccore.opt,402).text }
  dccore.save
  if ($window($dccore.win)) {
    if (%panel != $dccore.opt(panel)) { dccore.rebuild }
    if ($dccore.opt(font)) { font $dccore.win $dccore.fontsize Lucida Console }
    if (%bg != $dccore.opt(bg)) { dccore.background }
    dccore.title
    dccore.panel
  }
}
on *:dialog:dccore.opt:sclick:501: {
  if ($did(dccore.opt,402).text != $null) { dccore.set bot $did(dccore.opt,402).text }
  dialog -x dccore.opt
  dccore pair
}
on *:dialog:dccore.opt:sclick:502: {
  dialog -x dccore.opt
  dccore unpair
}
