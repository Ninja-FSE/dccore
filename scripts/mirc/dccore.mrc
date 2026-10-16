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
;    And @DCCore-Downloads (#1022): what the bot is downloading from other
;    bots, what waits, and what finished - see its section below.
;
;    And a settings window (#1264): the bot's settings, as its dashboard's
;    Settings page has them - /dccore settings, or Settings... in the menus.
;
;    And DCCore Chat (#371): a second window for public chat with other
;    operators in the channels your bot is in, relayed by the bot - see
;    its section near the end of this file.
;
;    And a dark theme and icons (#1368): /dccore theme dark gives the
;    dialogs dark colours, and /dccore icons puts an icon on every menu
;    item of this script and every page of the control panel - see "Theme
;    and icons" below. dll\ beside this file holds the two DLLs it uses.
;
;    When mIRC starts, @DCCore opens by itself, minimised, and says what it
;    is waiting for until the console connects; Chat and Downloads can do
;    the same - "Open when mIRC starts" in /dccore options (#1201).
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
;            /dccore settings     the bot's own settings (#1264)
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
;    The dark theme and the icons (#1368) are extras on top of that: the
;    dark dialogs and the menu icons need dll\dccore_ui.dll (mIRC 7.45 or
;    later, 32-bit; Windows 10 for the dark title bars), the control
;    panel's page icons DCX as well. Without them the script works and
;    looks exactly as it did.
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
alias dccore.ver { return 1.20.0 }
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
; The channel itself goes through dccore.chan, its colour from Options (#1259).
alias dccore.in { if ($1 == $null) || ($1 == -) { return } | return $+($chr(160),in,$chr(160),$dccore.chan($1)) }

alias dccore.init {
  if (!$hget(dccore)) { hmake dccore 32 }
  if (!$hget(dccore.live)) { hmake dccore.live 64 }
  ; #371 follow-up: the DCCore bots WHO has found, one entry per channel -
  ; kept apart from dccore.live so redrawing the side-listbox is a plain
  ; loop over chat.chans, not a scan of every key in that bigger table.
  if (!$hget(dccore.chatpeers)) { hmake dccore.chatpeers 32 }
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
  dccore.default dlfinished 15
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
  ; The nick, the channel and the search term inside a feed line (#1259).
  ; -1 is "same as the line": no colour codes at all. The term's "name" is the
  ; File names colour. The nick's "per" gives each nick a colour of its own -
  ; see dccore.pernick. Each has a colour of its own by default (#1348): with
  ; them all "same as the line", a busy window was nearly all black.
  dccore.inlinecolours
  dccore.default col.term 12
  dccore.default col.nick 6
  dccore.default col.chan 10
  ; DCCore Chat (#371): only the channels ticked in its window, until
  ; "all my channels" is chosen; the window opens by itself for a line
  ; Listening on every channel is the default (#958 follow-up): only lines
  ; from other DCCore bots arrive at all, so there is little to filter
  dccore.default chat.all 1
  dccore.default chat.popup 1
  ; Its history file in mIRC's log folder, and the window flashing for a
  ; line while it is not the active one (#1393): both on unless turned off
  ; in the window's right-click menu.
  dccore.default chat.history 1
  dccore.default chat.flash 1
  ; Which windows open, minimised, when mIRC starts (#1201). @DCCore does by
  ; default, so the bot's window is there before anything has connected;
  ; Chat and Downloads only when asked for, as before.
  dccore.default start.main 1
  dccore.default start.chat 0
  dccore.default start.downloads 0
  ; The settings window draws with DCX when it can (#1288); on forces the
  ; built-in window.
  dccore.default nodcx 0
  ; The theme and the icons (#1368): auto follows mIRC's own dark mode;
  ; icons puts them in the menus (drawn by DCX) and the control panel.
  dccore.default theme auto
  dccore.default icons 1
}
alias dccore.default { if ($hget(dccore,$1) == $null) { hadd dccore $1 $2- } }
; #1348: the colours inside a line had no colour of their own until 1.20.0.
; Every script before it saved them with the rest of the table, so a new
; default alone would reach new installs only. Once, each of the three that
; still holds its old default moves to the new one; a colour somebody picked
; stays. The mark is saved with it, so "same as the line" picked again later
; is never moved a second time.
alias dccore.inlinecolours {
  if ($hget(dccore,inlinecolours) != $null) { return }
  if ($hget(dccore,col.term) == name) { hadd dccore col.term 12 }
  if ($hget(dccore,col.nick) == -1) { hadd dccore col.nick 6 }
  if ($hget(dccore,col.chan) == -1) { hadd dccore col.chan 10 }
  hadd dccore inlinecolours 1
  if ($isfile($dccore.ini)) { dccore.save }
}
alias dccore.save { hsave -o dccore $dccore.ini }
alias dccore.set { hadd dccore $1 $2- | dccore.save }
alias dccore.forget { hdel dccore $1 | dccore.save }

on *:START: {
  dccore.init
  dccore.atstart
  dccore.theme.start
}
; The windows ticked under "Open when mIRC starts" (#1201): each minimised,
; its button at the end of the switchbar, so they are there without taking
; the focus. Only the windows: nothing here dials the bot or sets wantopen.
; The usual auto-connect on CONNECT fills @DCCore, which is already open by
; then. Until it does, @DCCore says in one line what it is waiting for.
alias dccore.atstart {
  if ($dccore.opt(start.main)) && (!$window($dccore.win)) {
    dccore.window start
    if ($dccore.st(state) == $null) { dccore.sys $dccore.waiting }
  }
  if ($dccore.opt(start.chat)) { dccore.chat.window start }
  if ($dccore.opt(start.downloads)) { dccore.dl.window start }
}
on *:LOAD: {
  dccore.init
  echo 14 -a DCCore window script $dccore.ver loaded. Type /dccore pair <botnick> to connect for the first time, /dccore for help.
  dccore.theme.start
}
on *:UNLOAD: {
  dccore.timers.off
  dccore.theme.stop
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
    if ($dccore.st(state) == in) { dccore.send pair $dccore.client $dccore.ver | hadd dccore.live pairsent 1 | return }
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
  if (%cmd == settings) { dccore.settings | return }
  if (%cmd == dcx) {
    if ($2 == check) { dccore.dcx.check }
    else { dccore.dcx.status }
    return
  }
  ; The theme and the icons (#1368): change, or say how they stand
  if (%cmd == theme) {
    if ($istok(auto dark light,$2,32)) { dccore.set theme $2 | dccore.theme.refresh }
    elseif ($2 != $null) { dccore.sys Use: /dccore theme auto, dark or light. | return }
    dccore.theme.status
    return
  }
  if (%cmd == icons) {
    if ($istok(on off,$2,32)) { dccore.set icons $iif($2 == on,1,0) | dccore.theme.refresh }
    elseif ($2 != $null) { dccore.sys Use: /dccore icons on or off. | return }
    dccore.theme.status
    return
  }
  if (%cmd == lists) { dccore.send lists | return }
  if (%cmd == fetch) { dccore.send fetch $2- | return }
  if (%cmd == window) { dccore.window | window -a $dccore.win | return }
  if (%cmd == downloads) { dccore.dl.window | return }
  if (%cmd == weburl) {
    if ($2 == $null) { dccore.dl.askweb | return }
    dccore.set weburl $2
    dccore.sys The dashboard is at $2 $+ .
    return
  }
  if (%cmd == chat) {
    if ($2 == $null) { dccore.chat.window | window -a $dccore.chat.win | return }
    dccore.chat.say $2-
    return
  }
  if (%cmd == status) { dccore.send status | return }
  ; The #1009 review: the hello-time warning told an operator
  ; whose feed was off to run this, and it did not exist - there was no
  ; %cmd branch for it, so it fell through to the help text instead.
  if (%cmd == consolefeed) { dccore.send consolefeed $2- | return }
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
  echo 14 -a $dccore.nbsp $+ $dccore.nbsp /dccore options $+ $str($dccore.nbsp,14) what to show, colours, panel, title bar, beep, windows at start
  echo 14 -a $dccore.nbsp $+ $dccore.nbsp /dccore settings $+ $str($dccore.nbsp,13) the bot's settings, as its dashboard's Settings page has them
  echo 14 -a $dccore.nbsp $+ $dccore.nbsp /dccore dcx [check] $+ $str($dccore.nbsp,9) whether the settings window can use DCX, and why not; check tests it here
  echo 14 -a $dccore.nbsp $+ $dccore.nbsp /dccore theme <mode> $+ $str($dccore.nbsp,9) auto, dark or light: the dialogs, and colours readable on dark
  echo 14 -a $dccore.nbsp $+ $dccore.nbsp /dccore icons on|off $+ $str($dccore.nbsp,9) icons in this script's menus and the control panel's page list
  echo 14 -a $dccore.nbsp $+ $dccore.nbsp /dccore window $+ $str($dccore.nbsp,15) open or focus @DCCore
  echo 14 -a $dccore.nbsp $+ $dccore.nbsp /dccore downloads $+ $str($dccore.nbsp,10) open the downloads window: coming in, waiting, finished
  echo 14 -a $dccore.nbsp $+ $dccore.nbsp /dccore weburl [addr] $+ $str($dccore.nbsp,7) where the bot's dashboard is, for the window's menu
  echo 14 -a $dccore.nbsp $+ $dccore.nbsp /dccore chat [text] $+ $str($dccore.nbsp,9) open DCCore Chat, or say something in it (public)
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
  hadd dccore.live vetted 0
  hdel dccore.live pairsent
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
  ; A @DCCore opened at start said "not connected to <network> yet" (#1201);
  ; now it is, so the title says what it waits for next. Only the title:
  ; the dial above is what fills the window.
  if ($dccore.here) { dccore.title }
}

on *:CHATCLOSE: {
  if ($nick != $dccore.bot) { return }
  var %was = $dccore.st(state)
  hadd dccore.live state closed
  hdel dccore.live mode
  hdel dccore.live pairsent
  hdel dccore.live vetted
  hdel dccore.live typed
  .timerdccoreOpen off
  .timerdccoreHB off
  .timerdccoreHello off
  dccore.sys Console closed $+ $iif(%was == in,$chr(32) $+ after $duration($calc($ctime - $dccore.st(opened)))) $+ .
  dccore.title
  dccore.chat.title
  dccore.panel
  dccore.dl.draw
  hdel dccore.live sw.unlocked
  dccore.sw.lost
  if (%was == taken) {
    ; another client took the session; reconnecting now would only take
    ; it straight back and the two of you would trade it for ever
    dccore.sys Not reconnecting by itself: another client took over the console. /dccore connect takes it back.
    return
  }
  if (%was == in) { hadd dccore.live tries 0 }
  dccore.retry
}
; $true when the console is up: logged in, and a chat to the bot is open
; (#1396). The state alone outlives a chat that ended without the CHATCLOSE
; above (or a table that outlived its session), and the control panel's
; start page read a stopped bot as connected from it. Nested, as mIRC
; evaluates every identifier in an if-line.
alias dccore.linked {
  if ($dccore.st(state) != in) { return $false }
  if ($dccore.bot == $null) { return $false }
  return $iif($chat($dccore.bot),$true,$false)
}
; A state that says logged in with no chat to the bot is put right as the
; CHATCLOSE above does it, without its message, its retry or its timers
; (#1396): nothing dials from here. The titles and the side panel are
; redrawn so they stop showing the old session; none of them dials.
alias dccore.unstale {
  if ($dccore.st(state) != in) { return }
  if ($dccore.linked) { return }
  hadd dccore.live state closed
  hdel dccore.live mode
  hdel dccore.live pairsent
  hdel dccore.live vetted
  hdel dccore.live typed
  hdel dccore.live sw.unlocked
  dccore.title
  dccore.chat.title
  dccore.panel
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

; A password typed at the prompt (#1273). $1- has its runs of spaces closed
; up and its leading and trailing ones dropped, so a password that has them
; never arrived as typed. Such a password is read from the editbox as it
; was typed and sent as DCCORE PASSWORD <value>, its spaces escaped as the
; settings window escapes a value; the bot decodes it. Nested, never put in
; a /var, which would close the spaces up again. Every other password goes
; as it always did, which a bot from before this still accepts - and so
; does one whose editbox does not hold the line $1- came from.
alias dccore.sendpass {
  if ($gettok($editbox(@DCCore),1-,32) === $1-) && ($len($editbox(@DCCore)) > $len($1-)) {
    dccore.send DCCORE PASSWORD $dccore.sw.enc($editbox(@DCCore))
    return
  }
  dccore.send $1-
}

; ---------------------------------------------------------------------
;  Reading what the bot says
; ---------------------------------------------------------------------

alias dccore.line {
  var %state = $dccore.st(state)
  var %text = $strip($1-)
  ; The structured feed comes only after the login: a DCCORE line before
  ; it is not the bot's, whoever sent it.
  if ($1 == DCCORE) {
    if (%state == in) { dccore.structured $2- }
    return
  }

  if (%text == Enter Your Password:) {
    if ($dccore.opt(token) != $null) && (!$dccore.st(tokentried)) && (!$dccore.st(tokenbad)) && (!$dccore.st(pairing)) {
      ; The token is only ever handed to the address the bot was paired from.
      ; Anyone on the network can take the bot's nick while it is away, and
      ; the script dials that nick by itself - so who answers is checked
      ; first, not assumed.
      if (!$dccore.peerok) { return }
      hadd dccore.live vetted 1
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
      hadd dccore.live vetted 1
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
    if ($dccore.st(pairing)) { dccore.send pair $dccore.client $dccore.ver | hadd dccore.live pairsent 1 }
    dccore.title
    dccore.chat.title
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
  dccore.chat.title
}
alias dccore.plain {
  .timerdccoreHello off
  if ($dccore.st(state) != in) { return }
  if ($dccore.st(mode) == structured) { return }
  if ($dccore.st(mode) == plain) { return }
  hadd dccore.live mode plain
  dccore.sys Plain mode: this bot does not send the structured feed, so there is no panel; the chat is shown as it comes. Console commands still work.
  dccore.title
  dccore.chat.title
  dccore.panel
  ; no HELLO, so no dccore.sw.lost: the start page says the link is up (#1396)
  dccore.sw.home.fill
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
    ; #982 audit finding 3: the chat window's title only followed a send-
    ; target change or its own creation, so it could go on saying "not
    ; connected to the bot" for a whole session if the window was opened
    ; before this HELLO landed - chat worked, the title just never said so.
    dccore.chat.title
    ; a Downloads window left open across a reconnect asks again (#1022)
    if ($window($dccore.dl.win)) { hdel dccore.live dlend | dccore.dl.tell | dccore.dl.draw }
    ; an open settings window starts again on the new console (#1264),
    ; which is locked until unlocked, whatever the one before was
    hdel dccore.live sw.unlocked
    dccore.sw.lost
    return
  }
  if (%type == STATUS) { dccore.status $2- | return }
  ; PING: the bot could not read its figures in time and sent this so the
  ; link is heard from; the heartbeat timer above was reset by it already
  if (%type == PING) { return }
  ; The bot's own CHECK_FOR_UPDATES, sent after every HELLO and after
  ; `checkupdates on|off` completes (#572 follow-up). Kept in dccore.live,
  ; not dccore - it is the bot's state, not a local preference, and would
  ; read stale (from a different bot, or a setting changed elsewhere) if
  ; saved into dccore.ini.
  if (%type == CHECKUPDATES) { hadd dccore.live checkupdates $2 | return }
  ; The bot's own DEBUG_TO_CONSOLE, sent after every HELLO and after
  ; `consolefeed on|off` completes (#1006 follow-up). Same reasoning as
  ; CHECKUPDATES above - it is the bot's state, kept in dccore.live, never
  ; dccore.ini. The plain-text warning that goes with it when it is off
  ; arrives as its own OUT line (#709's path), so it needs no handling here.
  if (%type == CONSOLEFEED) { hadd dccore.live consolefeed $2 | return }
  ; DCCore Chat (#371): a line said in one of the bot's channels, and the
  ; channels it can chat in (after HELLO, and on `chat` with nothing after it).
  if (%type == CHAT) { dccore.chat.feed $2- | return }
  ; <days> <max_mb>: how long and how much chat history is kept (#1401),
  ; the bot's settings; after HELLO and when they change
  if (%type == CHATHIST) { dccore.chat.hist.limits $2- | return }
  if (%type == CHANNELS) { dccore.chat.channels $2- | return }
  if (%type == PEERS) { dccore.chat.peerline $2- | return }
  if (%type == SLOT) { hadd dccore.live slot. $+ $dccore.st(nslots) $2- | hinc dccore.live nslots | dccore.panel.soon | return }
  if (%type == QUEUE) { hadd dccore.live queue. $+ $2 $3- | dccore.panel.soon | return }
  ; <bot> <received> <total> <bps> <name>: one file the bot is leeching now
  ; (#1019), part of the same burst as SLOT; the panel's Downloading section.
  if (%type == FETCHING) { hadd dccore.live fetch. $+ $dccore.st(nfetch) $2- | hinc dccore.live nfetch | dccore.panel.soon | return }
  ; <files> <folders> <bytes> <rebuilt_at> <rebuild_secs> <primary> <name>: one
  ; list's totals (#1359), in every burst once `library` was sent, the name
  ; last as it may hold spaces; SEARCHINDEX <rows> <lists> <state> after
  ; them. For the control panel's start page; cleared by every STATUS.
  if (%type == LIBRARY) { if ($dccore.st(nlib) == $null) { hadd dccore.live nlib 1 } | hadd dccore.live lib. $+ $dccore.st(nlib) $2- | hinc dccore.live nlib | return }
  if (%type == SEARCHINDEX) { hadd dccore.live libindex $2- | dccore.panel.soon | return }
  ; <phase> <folder_index> <folder_count> <files> <elapsed>: a list rebuild is
  ; running (#1024), however it was started; `end` when it stops. Kept in
  ; dccore.live and cleared by every STATUS, so a missed `end` lasts one burst.
  ; The background audio reading (#1182) comes the same way, as phase
  ; `reading <read> <to_read> <files_a_second> <elapsed>`, `finding` while a
  ; reading started alone reads the list to find what to read, then
  ; `rewriting` while it writes the lengths into the list - to 1.12 or later.
  if (%type == REBUILD) {
    if ($2 == end) { hdel dccore.live rebuild }
    else { hadd dccore.live rebuild $2- }
    dccore.title
    dccore.panel.soon
    return
  }
  ; <nick> <done> <total> <elapsed> <folder>: a folder pack is running (#1202);
  ; `end` when it stops. <done> is the archive's size so far, <total> the
  ; folder's (0 until measured), the folder last as it may hold spaces. Kept in
  ; dccore.live and cleared by every STATUS, like REBUILD.
  if (%type == PACKING) {
    if ($2 == end) { hdel dccore.live packing }
    else { hadd dccore.live packing $2- }
    dccore.title
    dccore.panel.soon
    return
  }
  ; The Downloads window's snapshot (#1022): DLBEGIN, one DLROW per download
  ; (<id> <kind> <state> <bot> <received> <total> <bps> <when> <note> <name>),
  ; DLEND <waiting_total> <complete_total> <failed_total>. Drawn at DLEND only, so a window
  ; is never drawn from half a snapshot.
  if (%type == DLBEGIN) { hdel -w dccore.live dl.* | hadd dccore.live dln 1 | return }
  if (%type == DLROW) { hadd dccore.live dl. $+ $dccore.st(dln) $2- | hinc dccore.live dln | return }
  if (%type == DLEND) { hadd dccore.live dlwait $2 | hadd dccore.live dlfin $3 | hadd dccore.live dlfail $4 | hadd dccore.live dlend 1 | dccore.dl.draw | return }
  if (%type == DQBEGIN) { hdel -w dccore.live dq.* | hdel dccore.live dqend | hadd dccore.live dqn 1 | return }
  if (%type == DQROW) { hadd dccore.live dq. $+ $dccore.st(dqn) $2- | hinc dccore.live dqn | return }
  if (%type == DQEND) { hadd dccore.live dqcount $2 | hadd dccore.live dqend 1 | dccore.dq.draw | return }
  ; The settings window's lines (#1264), but only the ones it is waiting
  ; for (and, for a few seconds after it closes, its late replies): the
  ; same lines answering a command typed in this window - settings, set,
  ; served - are shown here like any other, and leave the window alone.
  if ($istok($dccore.sw.types,%type,32)) && (($dccore.sw.wants(%type)) || ($dccore.st(sw.tail))) { dccore.sw.line $1- | return }
  ; The lock (#1264): a console logged in with the paired token may read
  ; the settings but not change them until `unlock <password>`. What the
  ; window did not ask for is said here.
  if (%type == LOCKED) { dccore.sys $dccore.bot $+ : $iif($istok(commit resend,$3,32),$4-,$3-) | return }
  if (%type == UNLOCKED) { hadd dccore.live sw.unlocked 1 | dccore.sys Unlocked: this console can change the bot's settings until it closes. | return }
  if (%type == UNLOCK) { dccore.sys Not unlocked: $3- | return }
  if (%type == SETAPPLIED) { dccore.sys The saved settings are in effect now. | return }
  ; an older bot's "Unknown command: consolecaps" is the window's answer
  if (%type == OUT) {
    if ($dccore.sw.old($2-)) { return }
    dccore.out $2-
    return
  }
  if (%type == LISTFETCH) {
    ; <bot> <auto|arrived|unusable> <text>: a held bot list asked for again,
    ; arrived, or not usable. The text already names the bot.
    dccore.msg $dccore.tag(LISTS,search) $4-
    return
  }
  if (%type == FETCH) {
    ; <bot> <asked|queued|receiving|done|failed> <text>: a file the bot itself
    ; is leeching from another bot (#1019). The text already names the bot and
    ; the file. These live in the @DCCore-Downloads window's log (#1022), not
    ; here; with that window closed they are not shown.
    dccore.dl.log $3 $4-
    return
  }
  if (%type == TAKEN) {
    dccore.sys $dccore.bot $+ : another client ( $+ $2 $+ ) took over the console.
    hadd dccore.live state taken
    return
  }
  if (%type == DROPPED) {
    dccore.alert $dccore.tag(DROPPED,fail) $2 line(s) were dropped by the bot: this client fell behind.
    return
  }
  if (%type == TOKEN) {
    ; Only the answer to a pair request this script sent in this chat, to a
    ; peer it had reason to trust: one at the host the bot was paired from
    ; (peerok said yes), or one the operator typed the password to. Anyone
    ; can take the bot's nick while it is away and answer the script's own
    ; dial; a token taken from them would replace the real one and re-point
    ; bothost at their host, and the next login would go to them.
    if (!$dccore.st(pairsent)) { dccore.sys Ignored a pairing token this script did not ask for. To pair this script, /dccore pair. | return }
    if (!$dccore.st(vetted)) && (!$dccore.st(typed)) { dccore.sys Ignored a pairing token from a chat that was never checked to be the bot. | return }
    hdel dccore.live pairsent
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
    dccore.msg $dccore.tag(REQUEST,request) $dccore.nick($2) $+ $dccore.in($3) asked for $iif($4 == folder,the folder) $dccore.name($5-)
    return
  }
  if (%type == QUEUED) {
    if (!$dccore.opt(show.queued)) { return }
    dccore.msg $dccore.tag(QUEUED,queued) $dccore.name($7-) for $dccore.nick($2) $+ $dccore.in($3) at # $+ $4 ( $+ $5 $+ / $+ $6 slots busy)
    return
  }
  if (%type == SENDING) {
    if (!$dccore.opt(show.sends)) { return }
    dccore.msg $dccore.tag(SENDING,sends) $dccore.name($7-) to $dccore.nick($2) $+ $dccore.in($3) (slot $4 $+ / $+ $5 $+ , $dccore.bytes($6) $+ )
    return
  }
  if (%type == RESUMED) {
    if (!$dccore.opt(show.sends)) { return }
    dccore.msg $dccore.tag(RESUMED,sends) $dccore.name($6-) for $dccore.nick($2) $+ $dccore.in($3) at $dccore.bytes($4) of $dccore.bytes($5)
    return
  }
  if (%type == SENT) {
    if (!$dccore.opt(show.sends)) { return }
    dccore.msg $dccore.tag(SENT,sends) $dccore.name($7-) to $dccore.nick($2) $+ $dccore.in($3) $+ : $dccore.bytes($4) in $dccore.dur($5) $iif($6 > 0,at $dccore.speed($6),at n/a)
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
    dccore.alert $dccore.tag(FAILED,fail) $dccore.name(%name) to $dccore.nick($2) $+ $dccore.in($3) - %why ( $+ $dccore.bytes($4) of $dccore.bytes($5) arrived)
    if ($dccore.opt(beep)) { beep 2 200 }
    return
  }
  if (%type == SEARCH) {
    hinc dccore.live searches
    if (!$dccore.opt(show.search)) { return }
    dccore.msg $dccore.tag(SEARCH,search) $dccore.nick($2) $+ $dccore.in($3) searched $dccore.term($5-) -> $4 result(s)
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
  hdel -w dccore.live fetch.*
  hdel dccore.live rebuild
  hdel dccore.live packing
  hdel -w dccore.live lib.*
  hdel dccore.live libindex
  hadd dccore.live nslots 1
  hadd dccore.live nfetch 1
  hadd dccore.live nlib 1
  dccore.title
  dccore.panel.soon
  ; Not while the side panel is shown (#1013): it carries the same figures,
  ; live, and the text filled with identical lines while nothing happened.
  ; A window without the panel keeps the line - there it is the only place
  ; the numbers are.
  if ($dccore.opt(statusmin) > 0) && (!$dccore.opt(panel)) && ($calc($ctime - $dccore.st(lastecho)) >= $calc($dccore.opt(statusmin) * 60)) {
    hadd dccore.live lastecho $ctime
    dccore.echo $dccore.tag(STATUS,info) slots $1 $+ / $+ $2 $dccore.dot queue $4 ( $+ $3 files) $dccore.dot today $5 files / $dccore.bytes($6) $dccore.dot $dccore.speed($7) now, record $dccore.speed($8)
  }
}

; ---------------------------------------------------------------------
;  Drawing: the text, the title bar, the side panel
; ---------------------------------------------------------------------

; What a rebuild has reached, for the title bar: "rebuilding folder 7/20" (or
; the phase alone when it has no folders to count). The background audio
; reading (#1182): "audio info 3,200/12,000".
alias dccore.rebuild.short {
  var %l = $dccore.st(rebuild)
  var %n = $gettok(%l,3,32)
  if ($gettok(%l,1,32) == reading) { return audio info $dccore.num($gettok(%l,2,32)) $+ / $+ $dccore.num(%n) }
  if ($gettok(%l,1,32) == rewriting) { return audio info: writing the list }
  if ($gettok(%l,1,32) == finding) { return audio info: finding what to read }
  return rebuilding $iif(%n > 0,folder $gettok(%l,2,32) $+ / $+ %n,$gettok(%l,1,32))
}

; $1 = start: opened when mIRC starts (#1201), so minimised (-n) with its
; button at the end of the switchbar (-z), and otherwise the same window.
alias dccore.window {
  if ($window($dccore.win)) { return }
  if ($1 == start) {
    if ($dccore.opt(panel)) { window -enzl30 $dccore.win }
    else { window -enz $dccore.win }
  }
  elseif ($dccore.opt(panel)) { window -el30 $dccore.win }
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
alias dccore.col { return $base($dccore.theme.legible($dccore.opt(col. $+ $1),win),10,10,2) }
alias dccore.name { return $+($chr(3),$dccore.col(name),",$1-,",$chr(15)) }
; The nick, the channel and the search term of a feed line (#1259), each in
; its own colour from Options. Every line that names a nick or a channel
; goes through these, so one choice colours them all alike.
;
; dccore.paint: $1 the colour, $2 the text. Anything that is not a colour
; number - -1, "same as the line" - returns the text with no codes at all, so the
; line reads exactly as it did before these options. A colour is always two
; digits: ^C3 followed by a nick like 3bot would read as colour 33. The
; $chr(15) after it ends the span the way the tag and the file name end
; theirs: the rest of the line has no colour of its own to return to.
alias dccore.paint {
  if ($1 !isnum 0-15) { return $2 }
  return $+($chr(3),$base($dccore.theme.legible($1,win),10,10,2),$2,$chr(15))
}
alias dccore.nick {
  var %c = $dccore.opt(col.nick)
  if (%c == per) { %c = $dccore.pernick($1) }
  return $dccore.paint(%c,$1)
}
alias dccore.chan { return $dccore.paint($dccore.opt(col.chan),$1) }
; The searched term, in quotes like a file name. "name" is the File names
; colour, which is what the term had before it had a choice of its own.
alias dccore.term {
  if ($dccore.opt(col.term) == name) { return $dccore.name($1-) }
  return $dccore.paint($dccore.opt(col.term),$+(",$1-,"))
}
; "per nick": the nick's own colour, from the first six hex digits of the
; MD5 of the nick in lower case - the same nick the same colour every time,
; whatever its case, on every machine. Picked from the colours that can be
; read on the window's background (dccore.nickpal).
alias dccore.pernick {
  var %pal = $dccore.nickpal
  var %n = $base($left($md5($lower($1)),6),16,10)
  return $gettok(%pal,$calc((%n % $numtok(%pal,32)) + 1),32)
}
; The colours a nick may take, one list per background colour 0-15 (with
; "none" in Options, mIRC's own background colour). Never white or black -
; one of them is the line's own text colour - nor the background itself,
; and only colours with a contrast of at least 3 to 1 against it (the WCAG
; figure for large text, from the palette in dccore.rgb). Worked out once
; and written here: the test re-computes it from dccore.rgb.
alias dccore.nickpal {
  var %bg = $dccore.opt(bg)
  ; mIRC's dark mode (#1368) draws on black, whatever $color says
  if (%bg !isnum 0-15) && ($dccore.theme.mircdark) { %bg = 1 }
  if (%bg !isnum 0-15) { %bg = $color(background) }
  if (%bg !isnum 0-15) { %bg = 0 }
  return $gettok(02 03 04 05 06 10 12 13 14/03 04 07 08 09 10 11 13 14 15/03 04 07 08 09 10 11 13 14 15/02 08 11/02 08 11/07 08 09 11 13 15/08 09 11 15/02 05 12/02 03 04 05 06 10 12 14/02 05 06 12/02 08/02 03 04 05 06 12 14/07 08 09 11 15/02 05/02 08 11/02 05 06 12,$calc(%bg + 1),47)
}
; Bytes as people read them - 27.5MB, 1.06MB/s - formatted here rather
; than by $bytes().suffix, which on the first real run gave "27.5" and
; "1.06/s" with no unit at all. Two decimals up to 10, one up to 100,
; none above, so a column stays a column.
alias dccore.bytes {
  var %n = $1
  if (%n !isnum) { %n = 0 }
  ; a library's size runs to terabytes (#1359): 21111GB read as a number, not a size
  if (%n >= 1099511627776) { return $+($dccore.round($calc(%n / 1099511627776)),TB) }
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
; a whole number with thousands separators: 312000 -> 312,000
alias dccore.num {
  var %n = $int($1), %o = $null
  while (%n >= 1000) {
    %o = $+($chr(44),$base($calc(%n % 1000),10,10,3),%o)
    %n = $int($calc(%n / 1000))
  }
  return $+(%n,%o)
}
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
; Activity - somebody asked for, got or searched for something. The
; window's button takes the MESSAGE colour, as a channel's does when
; somebody speaks (#892). A plain /echo is an EVENT line, which is why
; @DCCore never turned red however much happened in it; -m makes it a
; message. dccore.echo above stays the event line, for what a channel
; would also treat as one: the STATUS heartbeat (every few minutes -
; red for that would mean red always), joins, parts and bans.
; mIRC's help does not date -m or /window -g, so both are gated on mIRC 7
; per the note at the top: an older mIRC shows the line exactly as before.
alias dccore.msg {
  dccore.window
  if ($version >= 7) { echo -mti2 $dccore.win $iif($1- == $null,$dccore.nbsp,$1-) }
  else { echo -ti2 $dccore.win $iif($1- == $null,$dccore.nbsp,$1-) }
}
; A failure: the same, and the button in the HIGHLIGHT colour - the one a
; channel takes when somebody says your nick - so it stands out from
; ordinary activity (/window -g2). Not while you are looking at the
; window: mIRC does not colour the active window's button, and setting it
; there would leave it lit after you had already read the line.
alias dccore.alert {
  dccore.msg $1-
  if (($version >= 7) && ($active != $dccore.win)) { window -g2 $dccore.win }
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
  ; the control panel's start page follows the state too (#1396): the
  ; title is redrawn on every step of a dial. The timer dccore.panel.soon
  ; uses, so the two coalesce; before the return, as @DCCore may be closed
  if ($dialog(dccore.set)) { .timerdccoreSwHome -m 1 300 dccore.sw.home.fill }
  if (!$window($dccore.win)) { return }
  var %bot = $iif($dccore.bot,$dccore.bot,DCCore)
  var %net = $iif($network,$network,$server)
  ; No dial yet this session: say what the window is waiting for (#1201)
  if ($dccore.st(state) == $null) { titlebar $dccore.win %bot $dccore.dot $dccore.waiting | return }
  if ($dccore.st(state) != in) { titlebar $dccore.win %bot $dccore.dot $iif($dccore.st(state),$dccore.st(state),not connected) | return }
  if (!$dccore.opt(titlebar)) || ($dccore.st(mode) != structured) { titlebar $dccore.win %bot on %net | return }
  var %rb = $iif(($dccore.st(rebuild) != $null) && (!$dccore.opt(panel)),$dccore.dot $dccore.rebuild.short,)
  var %pk = $iif(($dccore.st(packing) != $null) && (!$dccore.opt(panel)),$dccore.dot packing $gettok($dccore.st(packing),5-,32))
  titlebar $dccore.win %bot on %net %rb %pk $dccore.dot slots $dccore.st(st.used) $+ / $+ $dccore.st(st.slots) $dccore.dot queue $dccore.st(st.qusers) $dccore.dot today $dccore.st(st.sent) files / $dccore.bytes($dccore.st(st.bytes)) $dccore.dot $dccore.speed($dccore.st(st.bps))
}

; What @DCCore is waiting for before its first dial of the session (#1201):
; a window opened at start would otherwise sit there saying only "not
; connected". The bot's network, not the active one: $dccore.cid finds it,
; and $scid(N).server is empty while that connection is not up.
alias dccore.waiting {
  if ($dccore.bot == $null) { return no bot paired yet: /dccore pair <botnick> }
  var %cid = $dccore.cid
  var %net = $iif($dccore.opt(net),$dccore.opt(net),IRC)
  if (%cid == $null) { return Waiting for the bot: not connected to %net yet }
  if ($scid(%cid).server == $null) { return Waiting for the bot: not connected to %net yet }
  if ($dccore.opt(wantopen)) && ($dccore.opt(auto)) { return Waiting for the bot: it is not answering yet }
  return Waiting for the bot: the console is not open - /dccore connect opens it
}

; SLOT and QUEUE lines arrive one by one after STATUS with no end marker,
; so the panel is redrawn a quarter of a second after the last of them.
; and the control panel's start page, drawn from the same figures (#1288)
alias dccore.panel.soon {
  .timerdccorePanel -m 1 250 dccore.panel
  if ($dialog(dccore.set)) { .timerdccoreSwHome -m 1 300 dccore.sw.home.fill }
}

; THE BUTTON KEEPS ITS COLOUR ACROSS A REDRAW (#1013). The panel is redrawn
; after every status burst - about every 30 seconds - and it starts with
; /clear -l, which in mIRC resets the window button's colour as well: a
; new line lit it red, the next redraw put it out, before anybody looked.
; So the colour is read before the redraw and set again after it
; (dccore.lit, dccore.relight), in dccore.panel below.
; What the button shows now, in /window -g's numbers: 2 the highlight colour,
; 1 the message colour, 0 anything else (the event colour cannot be set
; again, and is not what anybody watches for). mIRC's help says only that
; .sbcolor "returns the switchbar highlight color", so both a name and a
; number are understood. Nothing while the window is active: mIRC does not
; colour the active window's button.
alias dccore.lit {
  if (($version < 7) || ($active == $dccore.win)) { return 0 }
  var %c = $window($dccore.win).sbcolor
  if ((%c == 2) || (hi isin %c)) { return 2 }
  if ((%c == 1) || (mess isin %c)) { return 1 }
  return 0
}
alias dccore.relight {
  if (($1 isnum 1-2) && ($version >= 7) && ($active != $dccore.win)) { window -g $+ $1 $dccore.win }
}
alias dccore.panel {
  if (!$window($dccore.win)) { return }
  if (!$dccore.opt(panel)) { return }
  var %lit = $dccore.lit
  clear -l $dccore.win
  if ($dccore.st(mode) != structured) {
    aline -l 14 $dccore.win $iif($dccore.st(state) == in,(no panel in plain mode),(not connected))
    dccore.relight %lit
    return
  }
  var %head = $dccore.lcol(head)
  var %used = $dccore.st(st.used), %slots = $dccore.st(st.slots)
  aline -l %head $dccore.win Sending %used $+ / $+ %slots
  var %i = 1
  while ($dccore.st(slot. $+ %i) != $null) {
    var %l = $dccore.st(slot. $+ %i)
    ; <nick> <sent> <total> <bps> <name>
    var %pct = $iif($gettok(%l,3,32) > 0,$int($calc($gettok(%l,2,32) * 100 / $gettok(%l,3,32))),0)
    aline -l $dccore.lcol(sends) $dccore.win > $dccore.fit($gettok(%l,1,32),9) $dccore.rfit($dccore.bytes($gettok(%l,3,32)),7) $dccore.rfit(%pct $+ $chr(37),4) $dccore.rfit($dccore.speed($gettok(%l,4,32)),9)
    inc %i
  }
  if (%used < %slots) { aline -l 14 $dccore.win $dccore.nbsp $+ $dccore.nbsp ( $+ $calc(%slots - %used) free) }
  aline -l 14 $dccore.win $dccore.nbsp
  ; Downloading (#1019): what the bot itself is leeching from other bots. Only
  ; drawn while something is; a row starts with "<" (Sending's start with ">",
  ; which dccore.sels reads).
  if ($dccore.st(fetch.1) != $null) {
    var %nf = $calc($dccore.st(nfetch) - 1)
    aline -l %head $dccore.win Downloading %nf
    var %i = 1
    while ($dccore.st(fetch. $+ %i) != $null) {
      var %l = $dccore.st(fetch. $+ %i)
      ; <bot> <received> <total> <bps> <name>
      var %pct = $iif($gettok(%l,3,32) > 0,$int($calc($gettok(%l,2,32) * 100 / $gettok(%l,3,32))),0)
      aline -l $dccore.lcol(sends) $dccore.win < $dccore.fit($gettok(%l,1,32),9) $dccore.rfit($dccore.bytes($gettok(%l,3,32)),7) $dccore.rfit(%pct $+ $chr(37),4) $dccore.rfit($dccore.speed($gettok(%l,4,32)),9)
      inc %i
    }
    aline -l 14 $dccore.win $dccore.nbsp
  }
  ; Rebuilding (#1024): the phase, the folder it is on and the files so far.
  ; Only drawn while one runs.
  if ($dccore.st(rebuild) != $null) {
    var %r = $dccore.st(rebuild)
    ; Audio info (#1182): the background reading after a rebuild, or on its
    ; own from the Library menu - files read of files to read, and the rate.
    if ($istok(reading rewriting finding,$gettok(%r,1,32),32)) {
      aline -l %head $dccore.win Audio info
      if ($gettok(%r,1,32) == rewriting) { aline -l $dccore.lcol(sends) $dccore.win $dccore.nbsp $+ $dccore.nbsp writing the list }
      elseif ($gettok(%r,1,32) == finding) { aline -l $dccore.lcol(sends) $dccore.win $dccore.nbsp $+ $dccore.nbsp finding what to read }
      elseif ($gettok(%r,4,32) > 0) { aline -l $dccore.lcol(sends) $dccore.win $dccore.nbsp $+ $dccore.nbsp $dccore.num($gettok(%r,2,32)) $+ / $+ $dccore.num($gettok(%r,3,32)) read $dccore.dot $dccore.num($gettok(%r,4,32)) $+ /s }
      else { aline -l $dccore.lcol(sends) $dccore.win $dccore.nbsp $+ $dccore.nbsp $dccore.num($gettok(%r,2,32)) $+ / $+ $dccore.num($gettok(%r,3,32)) read }
    }
    else {
      aline -l %head $dccore.win Rebuilding $gettok(%r,1,32)
      if ($gettok(%r,3,32) > 0) { aline -l $dccore.lcol(sends) $dccore.win $dccore.nbsp $+ $dccore.nbsp folder $gettok(%r,2,32) $+ / $+ $gettok(%r,3,32) $dccore.dot $dccore.num($gettok(%r,4,32)) files }
      else { aline -l $dccore.lcol(sends) $dccore.win $dccore.nbsp $+ $dccore.nbsp $dccore.num($gettok(%r,4,32)) files }
    }
    if ($gettok(%r,5,32) > 0) { aline -l 14 $dccore.win $dccore.nbsp $+ $dccore.nbsp running $dccore.dur($gettok(%r,5,32)) }
    aline -l 14 $dccore.win $dccore.nbsp
  }
  ; Packing (#1202): the folder being packed into an archive, for whom, and how
  ; far the archive has got of the folder's size. Only drawn while one runs.
  if ($dccore.st(packing) != $null) {
    var %pk = $dccore.st(packing)
    aline -l %head $dccore.win Packing
    aline -l $dccore.lcol(sends) $dccore.win $dccore.nbsp $+ $dccore.nbsp $gettok(%pk,5-,32)
    if ($gettok(%pk,3,32) > 0) { aline -l $dccore.lcol(sends) $dccore.win $dccore.nbsp $+ $dccore.nbsp for $gettok(%pk,1,32) $dccore.dot $dccore.bytes($gettok(%pk,2,32)) of $dccore.bytes($gettok(%pk,3,32)) $dccore.dot $int($calc(100 * $gettok(%pk,2,32) / $gettok(%pk,3,32))) $+ $chr(37) }
    else { aline -l $dccore.lcol(sends) $dccore.win $dccore.nbsp $+ $dccore.nbsp for $gettok(%pk,1,32) $dccore.dot $dccore.bytes($gettok(%pk,2,32)) }
    if ($gettok(%pk,4,32) > 0) { aline -l 14 $dccore.win $dccore.nbsp $+ $dccore.nbsp running $dccore.dur($gettok(%pk,4,32)) }
    aline -l 14 $dccore.win $dccore.nbsp
  }
  aline -l %head $dccore.win Queue $dccore.st(st.qusers) $iif($dccore.st(st.qfiles) > 0,( $+ $dccore.st(st.qfiles) files))
  var %i = 1
  while ($dccore.st(queue. $+ %i) != $null) {
    var %l = $dccore.st(queue. $+ %i)
    ; <nick> <files> <frozen_secs_left>
    var %files = $gettok(%l,2,32)
    if ($gettok(%l,3,32) > 0) { aline -l $dccore.lcol(fail) $dccore.win $dccore.rfit(%i,2) $dccore.fit($gettok(%l,1,32),9) frozen $dccore.dur($gettok(%l,3,32)) }
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
  dccore.relight %lit
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
;  The Downloads window (#1022): what the bot is leeching from other bots
; ---------------------------------------------------------------------
;
;  Watching only. The bot sends a whole snapshot - DLBEGIN, a DLROW for each
;  download, DLEND - every few seconds, and only while this window is open:
;  the window says `downloads on <rows>` when it opens (and after a
;  reconnect) and `downloads off` when it closes. Searching other bots'
;  lists and adding to the queue stay on the dashboard; the menu opens it.
;
;  The rows are in the window's side listbox, like @DCCore's panel, so a row
;  can be selected and right-clicked. dlmap.<line> remembers which download
;  a line is: "<kind>:<state>:<id>" (one word, so it passes as one argument). The bars are # and - (plain ASCII).

alias dccore.dl.win { return @DCCore-Downloads }
alias dccore.dl.rows {
  var %n = $dccore.opt(dlfinished)
  return $iif(%n isnum 1-15,$int(%n),15)
}
alias dccore.dl.tell {
  if ($chat($dccore.bot)) && ($dccore.st(mode) == structured) { .msg $+(=,$dccore.bot) downloads on $dccore.dl.rows }
}
; $1 = start: opened when mIRC starts (#1201) - minimised, its button at the
; end of the switchbar, and an open one is left where it is, not brought up.
alias dccore.dl.window {
  if ($window($dccore.dl.win)) {
    if ($1 != start) { window -a $dccore.dl.win }
    return
  }
  if ($1 == start) { window -nzl64 $dccore.dl.win }
  else { window -l64 $dccore.dl.win }
  if ($dccore.opt(font)) { font $dccore.dl.win $dccore.fontsize Lucida Console }
  titlebar $dccore.dl.win DCCore Downloads $dccore.dot what $iif($dccore.bot,$dccore.bot,the bot) is fetching from other bots
  echo 14 -i2 $dccore.dl.win Every request, queue place, transfer and result appears here as it happens; the list on the right is how things stand now.
  echo 14 -i2 $dccore.dl.win Right-click a row there: cancel a request that is waiting, or download a failed one again. Searching other bots is on the dashboard (right-click, Open the dashboard).
  hdel dccore.live dlend
  dccore.dl.draw
  dccore.dl.tell
}
on *:CLOSE:@DCCore-Downloads: {
  if ($chat($dccore.bot)) && ($dccore.st(mode) == structured) { .msg $+(=,$dccore.bot) downloads off }
}

; One event of the fetch feed into this window's own log, tagged the way the
; feed reads: <asked|queued|receiving|done|failed> <text>.
alias dccore.dl.log {
  if (!$window($dccore.dl.win)) { return }
  var %tag = $dccore.tag(FETCH,sends)
  if ($1 == asked) { %tag = $dccore.tag(REQUEST,search) }
  elseif ($1 == queued) { %tag = $dccore.tag(QUEUE,queued) }
  var %text = $2-
  ; the bot's words are "Receiving ..." and "Fetched ..."; here they read as what they are
  if ($1 == receiving) { %tag = $dccore.tag(DOWNLOADING,sends) | %text = Started downloading $3- }
  elseif ($1 == done) { %tag = $dccore.tag(FINISHED,sends) | %text = Received $3- }
  elseif ($1 == failed) { %tag = $dccore.tag(FAILED,fail) }
  if ($version >= 7) { echo -mti2 $dccore.dl.win %tag %text }
  else { echo -ti2 $dccore.dl.win %tag %text }
}

; "<kind>:<state>:<id>" of the selected row, or nothing
alias dccore.dl.pick { return $hget(dccore.live,$+(dlmap.,$sline($dccore.dl.win,1).ln)) }
alias dccore.dl.do {
  var %p = $dccore.dl.pick
  if (%p != $null) { dccore.send $1 $gettok(%p,3,58) }
}
alias dccore.dl.askweb {
  var %u = $input(Address of the DCCore dashboard - for example http://host.example:8420/,eo,DCCore)
  if (%u != $null) { dccore.set weburl %u | dccore.sys The dashboard is at %u $+ . }
}
alias dccore.dl.web {
  if ($dccore.opt(weburl) == $null) { dccore.dl.askweb }
  var %u = $dccore.opt(weburl)
  if (%u == $null) { return }
  if ($left(%u,7) != http://) && ($left(%u,8) != https://) { %u = http:// $+ %u }
  run $qt(%u)
}

; A time for a finished row: the hour and minute, with the day when it is not today.
alias dccore.dl.when {
  if ($1 !isnum) || ($1 <= 0) { return --:-- }
  return $asctime($1,$iif($calc($ctime - $1) > 72000,ddd HH:nn,HH:nn))
}
alias dccore.dl.bar {
  var %pct = $1
  if (%pct > 100) { %pct = 100 }
  var %fill = $int($calc(%pct / 10))
  return $+($chr(91),$str($chr(35),%fill),$str($chr(45),$calc(10 - %fill)),$chr(93))
}

; One line into the listbox, remembering what it stands for.
alias dccore.dl.add {
  aline -l $1 $dccore.dl.win $3-
  if ($2 != -) { hadd dccore.live $+(dlmap.,$line($dccore.dl.win,0,1)) $2 }
}

alias dccore.dl.draw {
  if (!$window($dccore.dl.win)) { return }
  clear -l $dccore.dl.win
  hdel -w dccore.live dlmap.*
  var %w = $dccore.dl.win
  if ($dccore.st(mode) != structured) { aline -l 14 %w $iif($dccore.st(state) == in,(no downloads in plain mode),(not connected)) | return }
  if (!$dccore.st(dlend)) { aline -l 14 %w (asking the bot...) | return }
  var %head = $dccore.lcol(head)
  var %n = $calc($dccore.st(dln) - 1)
  var %nd = 0, %i = 1
  while (%i <= %n) {
    if ($gettok($dccore.st(dl. $+ %i),2,32) == d) { inc %nd }
    inc %i
  }
  var %nw = $dccore.st(dlwait), %nf = $dccore.st(dlfin), %nx = $dccore.st(dlfail)
  if (%nd == 0) && (%nw == 0) && (%nf == 0) && (%nx == 0) { aline -l 14 %w (nothing downloading, waiting or finished) | return }
  var %kind = none, %lastbot = $null, %ind = $dccore.nbsp $+ $dccore.nbsp
  %i = 1
  while (%i <= %n) {
    var %l = $dccore.st(dl. $+ %i)
    ; <id> <kind> <state> <bot> <received> <total> <bps> <when> <note> <name>
    var %k = $gettok(%l,2,32), %bot = $gettok(%l,4,32), %name = $gettok(%l,10-,32)
    ; the end of a long name is what tells two files apart
    if ($len(%name) > 44) { %name = .. $+ $right(%name,42) }
    var %note = $replace($gettok(%l,9,32),_,$dccore.nbsp)
    var %map = $+(%k,:,$gettok(%l,3,32),:,$gettok(%l,1,32))
    if (%k != %kind) {
      if (%kind != none) { dccore.dl.add 14 - $dccore.nbsp }
      %kind = %k
      %lastbot = $null
      if (%k == d) { dccore.dl.add %head - Downloading %nd }
      elseif (%k == w) { dccore.dl.add %head - Waiting %nw $iif(%nw > 50,( $+ showing the first 50 $+ )) }
      elseif (%k == c) { dccore.dl.add %head - Finished $iif(%nf > $dccore.dl.rows,(last $dccore.dl.rows of %nf),( $+ %nf $+ )) }
      else { dccore.dl.add %head - Failed $iif(%nx > $dccore.dl.rows,(last $dccore.dl.rows of %nx),( $+ %nx $+ )) }
    }
    ; a nick once, its downloads under it
    if (%bot != %lastbot) { %lastbot = %bot | dccore.dl.add $dccore.lcol(name) - %bot }
    if (%k == d) {
      var %got = $gettok(%l,5,32), %total = $gettok(%l,6,32)
      var %pct = $iif(%total > 0,$int($calc(%got * 100 / %total)),0)
      dccore.dl.add $dccore.lcol(sends) %map %ind $+ %name
      dccore.dl.add 14 %map %ind $+ $dccore.dl.bar(%pct) $dccore.rfit($iif(%total > 0,%pct $+ $chr(37),?),4) $dccore.rfit($dccore.speed($gettok(%l,7,32)),9) $dccore.rfit($dccore.bytes($iif(%total > 0,%total,%got)),7)
    }
    elseif (%k == w) {
      dccore.dl.add 14 %map %ind $+ %name
      dccore.dl.add $dccore.lcol(queued) %map %ind $+ %note
    }
    elseif (%k == c) {
      dccore.dl.add $dccore.lcol(sends) %map %ind $+ $dccore.fit(%name,44) $dccore.rfit($dccore.bytes($gettok(%l,5,32)),7) $dccore.dl.when($gettok(%l,8,32))
    }
    else {
      dccore.dl.add $dccore.lcol(fail) %map %ind $+ $dccore.fit(%name,44) $dccore.rfit($dccore.bytes($gettok(%l,5,32)),7) $dccore.dl.when($gettok(%l,8,32))
      dccore.dl.add $dccore.lcol(fail) %map %ind $+ %note
    }
    inc %i
  }
}

menu @DCCore-Downloads {
  $iif($gettok($dccore.dl.pick,1,58) == w,$dccore.mi(cancel) Cancel this request):dccore.dl.do dlcancel
  $iif($gettok($dccore.dl.pick,2,58) == failed,$dccore.mi(again) Download again):dccore.dl.do dlagain
  $dccore.mi(purge) Clear the finished ones...:dccore.confirm dlclear Clear the list of finished downloads? Only the list is cleared and no files are deleted.
  -
  $dccore.mi(dashboard) Open the dashboard in the browser:dccore.dl.web
  $dccore.mi(settings) Control Panel...:dccore.settings
  $dccore.mi(cancel) Close:window -c @DCCore-Downloads
}

; ---------------------------------------------------------------------
;  Download queues (#1217): the requests the bot has made that have not started
; ---------------------------------------------------------------------
;
;  A dialog with every request still waiting - a file, a !rar folder or a
;  bot's list - asked for, queued at the other bot or held back here. Select
;  some and remove them, or remove all of them; a download that has started is
;  never touched. The bot sends the rows when asked (`dlqueue`), and asked
;  again after each removal. dqmap.<line> remembers which request a line is.

alias dccore.queues {
  if ($dccore.st(mode) != structured) { dccore.sys Download queues needs the structured link to the bot. | return }
  if ($dialog(dccore.dq)) { dialog -v dccore.dq | dccore.dq.ask | return }
  dialog -m dccore.dq dccore.dq
}
alias dccore.dq.ask {
  hdel dccore.live dqend
  dccore.dq.draw
  if ($chat($dccore.bot)) { .msg $+(=,$dccore.bot) dlqueue }
}
alias dccore.dq.draw {
  if (!$dialog(dccore.dq)) { return }
  did -r dccore.dq 2
  hdel -w dccore.live dqmap.*
  if ($dccore.st(mode) != structured) { did -ra dccore.dq 1 (not connected to the bot) | return }
  if (!$dccore.st(dqend)) { did -ra dccore.dq 1 (asking the bot...) | return }
  var %n = $calc($dccore.st(dqn) - 1), %i = 1
  while (%i <= %n) {
    var %l = $dccore.st(dq. $+ %i)
    ; <id> <kind> <state> <bot> <note> <name>
    var %name = $gettok(%l,6-,32)
    if ($gettok(%l,2,32) == r) { %name = [!rar] %name }
    did -a dccore.dq 2 $gettok(%l,4,32) $dccore.dot %name $dccore.dot $replace($gettok(%l,5,32),_,$chr(32))
    hadd dccore.live $+(dqmap.,$did(dccore.dq,2).lines) $gettok(%l,1,32)
    inc %i
  }
  did -ra dccore.dq 1 $iif($dccore.st(dqcount) == 0,Nothing is waiting.,$dccore.st(dqcount) waiting - select the ones to remove (Ctrl or Shift for several))
}
; Up to 20 ids to a line, so a long selection stays inside one IRC line.
; $chat() checked the same way dccore.dq.ask already does (#1246 review):
; sending unconditionally meant a stale window open after a disconnect
; queued a DCC CHAT message nobody would ever read it as.
alias dccore.dq.cancel {
  if (!$chat($dccore.bot)) { return }
  var %ids = $1-, %batch = $null, %k = 0, %i = 1
  while (%i <= $numtok(%ids,32)) {
    %batch = %batch $gettok(%ids,%i,32)
    inc %k
    if (%k == 20) || (%i == $numtok(%ids,32)) { .msg $+(=,$dccore.bot) dlcancel %batch | %batch = $null | %k = 0 }
    inc %i
  }
}
alias dccore.dq.remove {
  var %n = $did(dccore.dq,2,0).sel, %i = 1, %ids = $null
  if (%n == 0) { did -ra dccore.dq 1 Select the requests to remove first. | return }
  while (%i <= %n) {
    var %id = $hget(dccore.live,$+(dqmap.,$did(dccore.dq,2,%i).sel))
    if (%id != $null) { %ids = %ids %id }
    inc %i
  }
  if (%ids == $null) { return }
  dccore.dq.cancel %ids
  dccore.dq.ask
}

dialog dccore.dq {
  title "DCCore - Download queues"
  size -1 -1 330 200
  option dbu
  text "", 1, 5 4 320 8
  list 2, 5 14 320 160, extsel hsbar vsbar
  button "Remove selected", 3, 5 181 62 12
  button "Remove all...", 4, 70 181 52 12
  button "Refresh", 5, 125 181 40 12
  button "Close", 6, 285 181 40 12, cancel
}
on *:dialog:dccore.dq:init:0: { dccore.dq.ask | dccore.theme.dialog dccore.dq init }
on *:dialog:dccore.dq:sclick:3: { dccore.dq.remove }
on *:dialog:dccore.dq:sclick:4: {
  if ($dccore.st(dqcount) == 0) || (!$dccore.st(dqend)) { return }
  if (!$chat($dccore.bot)) { return }
  if ($input(Remove all $dccore.st(dqcount) waiting requests? Downloads that have started are left alone.,yq,DCCore)) { .msg $+(=,$dccore.bot) dlcancel all | dccore.dq.ask }
}
on *:dialog:dccore.dq:sclick:5: { dccore.dq.ask }

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
    dccore.sendpass $1-
    dccore.echo $dccore.prompt ********
    halt
  }
  ; unlock <password> (#1264): the password is never shown
  dccore.echo $dccore.prompt $iif($1 == unlock,unlock ********,$1-)
  dccore.send $1-
  halt
}
on *:INPUT:@DCCore-console: {
  if ($left($1,1) == /) && ($left($1,2) != //) { return }
  echo -ti2 @DCCore-console $dccore.prompt $iif($1 == unlock,unlock ********,$1-)
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
; Cancel the running pack (#1202): top of the right-click menu while one runs; named, so the prompt can say whose.
alias dccore.packcancel {
  if ($dccore.st(packing) == $null) { return }
  if ($input(Cancel the pack of $gettok($dccore.st(packing),5-,32) for $gettok($dccore.st(packing),1,32) $+ ? The partial archive is deleted and the user is told.,yq,DCCore)) { dccore.send packcancel }
}
; Yes or no first, for the ones that change something or take a while.
alias dccore.confirm {
  if ($input($2-,yq,DCCore)) { dccore.send $1 }
}
; Ignore a nick for some minutes (#1206). The bot drops its requests until the
; time is up; Cancel or something that is not a whole number sends nothing.
; No commas in the prompt text: they would end the $input argument.
alias dccore.minutes {
  var %v = $input(Ignore $1 for how many minutes? 1 to 10080. Its requests are dropped until then.,eo,DCCore,30)
  if (%v isnum 1-10080) && (. !isin %v) { return %v }
  if (%v != $null) { dccore.sys Ignore: %v is not a whole number of minutes from 1 to 10080. }
}
alias dccore.ignorefor {
  var %m = $dccore.minutes($1)
  if (%m) { dccore.send ignore $1 %m }
}
; Drop what it has queued and what it asks for next: ignore first, so its
; pending replies go too, then clear - one console command, so the clear
; only happens if the ignore actually took (#1247). Two separate sends
; used to clear the queue unconditionally even when the bot refused the
; ignore (its own nick, or a nick outside the pattern ignore accepts).
alias dccore.clearignore {
  var %m = $dccore.minutes($1)
  if (%m) { dccore.send clearandignore $1 %m }
}
alias dccore.askfont {
  var %v = $input(Font size (6 or more),eo,DCCore)
  if (%v isnum) { dccore font %v }
}

; Every /dccore command, and every console command worth a click, is here.
menu @DCCore {
  $iif($dccore.st(packing) != $null,$dccore.mi(cancel) Cancel the running pack...):dccore.packcancel
  $dccore.mi(settings) Control Panel:dccore.settings
  $dccore.mi(console) Console command:dccore.askraw
  -
  $dccore.mi(version) Info
  .$dccore.mi(status) Status:dccore.send status
  .$dccore.mi(slots) Slots:dccore.send slots
  .$dccore.mi(queue) Queue:dccore.send queue
  .$dccore.mi(dlqueue) Download queues...:dccore.queues
  .$dccore.mi(uptime) Uptime:dccore.send uptime
  .$dccore.mi(version) Version:dccore.send version
  .-
  .$dccore.mi(help) Command list:dccore
  $dccore.mi(lists) Lists
  .$dccore.mi(lists) Show the lists:dccore lists
  .$dccore.mi(fetch) Fetch the changed lists:dccore fetch
  .$dccore.mi(ask) Ask a bot for its list...:dccore.ask fetch Ask which bot for its list
  .$dccore.mi(purge) Purge every held list...:if ($input(Forget every held bot list? Each rebuilds on its own - a changed advert or your next fetch - with its channel resolved fresh. Nothing of yours is touched.,yq,DCCore)) { dccore.send purgealllists confirm }
  $dccore.mi(library) Library
  .$dccore.mi(search) Find duplicate filenames:dccore.send verify
  .$dccore.mi(rebuild) Rebuild the list...:dccore.confirm update Rebuild the list? It walks the whole library and can take minutes.
  .$dccore.mi(audio) Read audio info:dccore.send audioinfo
  $dccore.mi(user) User control
  .$dccore.mi(ban) Bans:dccore.send bans
  .$dccore.mi(ban) Ban...:dccore.ask ban Ban pattern (for example *!*@host.example)
  .$dccore.mi(unban) Unban...:dccore.ask unban Pattern to remove
  .$dccore.mi(clear) Clear a queue...:dccore.ask clearqueue Clear the queue of which nick
  .$dccore.mi(ignore) Ignore a nick...:dccore.ask ignore Which nick and for how many minutes - for example someone 30
  .$dccore.mi(unban) Stop ignoring a nick...:dccore.ask unignore Stop ignoring which nick
  .$dccore.mi(unban) Unpause a nick...:dccore.ask unpause Unpause which nick - its sends failed too often today
  .$dccore.mi(moveup) Move in the queue...:dccore.ask queuemove Nick then up or down - or nick then file number then up or down
  .$dccore.mi(clear) Remove one queued file...:dccore.ask queueremove Nick then the file number from Queue of the nick
  $dccore.mi(rehash) Control
  .$dccore.mi(update) Check for a new version:dccore.send checkversion
  .$dccore.mi(update) Daily update check $iif($dccore.st(checkupdates) == on,off,on):dccore.send checkupdates $iif($dccore.st(checkupdates) == on,off,on)
  .$dccore.mi(console) Console feed $iif($dccore.st(consolefeed) == on,off,on):dccore.send consolefeed $iif($dccore.st(consolefeed) == on,off,on)
  .-
  .$dccore.mi(rehash) Reload the bot (rehash)...:dccore.confirm rehash Reload the bot's code and settings?
  .$dccore.mi(stop) Stop the bot...:if ($input(Stop the bot? It leaves IRC and ends; start it again with start-dccore.,yq,DCCore)) { dccore.send shutdown now }
  -
  $iif($dccore.selq,$dccore.mi(queue) Queue of $dccore.selq):dccore.send queue $dccore.selq
  $iif($dccore.selq,$dccore.mi(clear) Clear the queue of $dccore.selq):dccore.send clearqueue $dccore.selq
  $iif($dccore.selq,$dccore.mi(ignore) Ignore $dccore.selq for...):dccore.ignorefor $dccore.selq
  $iif($dccore.selq,$dccore.mi(ban) Clear the queue of $dccore.selq and ignore for...):dccore.clearignore $dccore.selq
  $iif($dccore.selq,$dccore.mi(moveup) Move $dccore.selq earlier in line):dccore.send queuemove $dccore.selq up
  $iif($dccore.selq,$dccore.mi(movedown) Move $dccore.selq later in line):dccore.send queuemove $dccore.selq down
  $iif($dccore.sels,$dccore.mi(queue) Queue of $dccore.sels):dccore.send queue $dccore.sels
  -
  $dccore.mi(connect) Connection
  .$iif($chat($dccore.bot),$dccore.mi(disconnect) Disconnect,$dccore.mi(connect) Connect):dccore $iif($chat($dccore.bot),disconnect,connect)
  .$dccore.mi(pair) Pair with the bot:dccore pair $dccore.bot
  .$dccore.mi(unpair) Forget the token (unpair):dccore unpair
  .$dccore.mi(trust) Trust the bot's host:dccore trust
  $dccore.mi(panel) Window
  .$dccore.mi(chat) DCCore Chat:dccore chat
  .$dccore.mi(downloads) Downloads window:dccore downloads
  .-
  .$dccore.mi(panel) Panel $iif($dccore.opt(panel),off,on):dccore panel $iif($dccore.opt(panel),off,on)
  .$dccore.mi(font) Font size...:dccore.askfont
  .$dccore.mi(dashboard) Dashboard address...:dccore weburl
  .$dccore.mi(clear) Clear window:clear @DCCore
  .-
  .$dccore.mi(palette) Theme
  ..$iif($dccore.theme.mode == auto,$style(1)) Follow mIRC:dccore theme auto
  ..$iif($dccore.theme.mode == dark,$style(1)) Dark:dccore theme dark
  ..$iif($dccore.theme.mode == light,$style(1)) Light:dccore theme light
  ..-
  ..$iif($dccore.theme.icons,$style(1)) Icons in the menus and the control panel:dccore icons $iif($dccore.theme.icons,off,on)
  $dccore.mi(purge) Clear finished...:dccore.confirm dlclear Clear the list of finished downloads? Only the list is cleared and no files are deleted.
}

menu nicklist {
  $dccore.mi(app) DCCore
  .$dccore.mi(queue) Queue of $1:dccore.send queue $1
  .$dccore.mi(clear) Clear the queue of $1:dccore.send clearqueue $1
  .$dccore.mi(ignore) Ignore $1 for...:dccore.ignorefor $1
  .$dccore.mi(ban) Clear the queue of $1 and ignore for...:dccore.clearignore $1
  .$dccore.mi(unban) Stop ignoring $1:dccore.send unignore $1
}

menu status,channel {
  $dccore.mi(app) DCCore
  .$dccore.mi(console) Open the window:dccore window
  .$dccore.mi(chat) Open DCCore Chat:dccore chat
  .$dccore.mi(downloads) Open the Downloads window:dccore downloads
  .$dccore.mi(dlqueue) Download queues...:dccore.queues
  .$dccore.mi(lists) Show the lists:dccore lists
  .$dccore.mi(fetch) Fetch the changed lists:dccore fetch
  .$dccore.mi(help) Command list:dccore
  .$iif($chat($dccore.bot),$dccore.mi(disconnect) Disconnect,$dccore.mi(connect) Connect):dccore $iif($chat($dccore.bot),disconnect,connect)
  .$dccore.mi(settings) Control Panel...:dccore.settings
}

; ---------------------------------------------------------------------
;  Theme and icons (#1368): the dark theme of the dialogs and the menus,
;  readable colours on a dark background, and icons in the menus and in
;  the control panel's page list
; ---------------------------------------------------------------------
;
;  /dccore theme auto|dark|light. auto follows mIRC's own dark mode -
;  $darkmode, mIRC 7.79 and later; an older mIRC has none, so there auto
;  is light and dark is chosen by hand. light leaves everything exactly as
;  it was before the theme existed: every call below does nothing then.
;
;  What dark changes
;    - The dialogs (the control panel, the list window, download queues):
;      the title bar, the background and mIRC's own controls, through
;      dll\dccore_ui.dll (dccore.theme.dialog). Not while mIRC itself is in
;      dark mode: it draws its dialogs dark already, and two hands on the
;      same controls is one too many. A dialog DCX has marked is passed with
;      "x": DCX paints its background and its own controls, and those take
;      their colours here instead - dccore.theme.sys for the lines, frames
;      and tabs as dccore.sw.dcx makes them, and the texts, the lists and
;      the background just after (dccore.theme.dcxset) - or, for what is
;      made later (a page's frames, the tables), as it is made.
;    - Not the right-click menus. Windows draws every menu of a program in
;      one style, so a dark menu here would be every menu in mIRC dark, and
;      other scripts' are not this script's to change. mIRC's own dark mode
;      (Options, Display - mIRC 7.79 and later) is exactly that, and auto
;      follows it.
;    - The colours in @DCCore, its panel, DCCore Chat and the Downloads
;      window, on a dark background - mIRC's dark mode, a dark colour scheme
;      (Alt+K) or a dark Background chosen in the mIRC settings: the colours
;      meant for a white one (navy, blue, maroon, purple, the dark green)
;      are drawn as their light neighbours (dccore.theme.legible). The
;      windows themselves keep mIRC's colours: mIRC has no per-window text,
;      listbox or editbox colour, so a dark window is mIRC's to give.
;
;  /dccore icons on|off: an icon in front of each of this script's menu
;  items, and of each page in the control panel's DCX page list
;  (dll\dccore_icons.dll). Only this script's items: dll\dccore_ui.dll hooks
;  the opening of a menu (MenuIcons), finds the items that begin with the
;  mark $dccore.mi gives them, takes the mark off and puts the icon beside
;  the text - as a plain menu bitmap, so the menu is still Windows' and
;  mIRC's own, every other item and every other script's menu untouched.
;
;  An item gets its icon from $dccore.mi(<name>) in front of its text: "<N>"
;  and $chr(11), N the icon's number counted from 1, while the hook is on -
;  and nothing otherwise, so a menu reads as it always did without it. A
;  checked item ($style(1)) has none: the check mark is its picture.
;
;  dccore_ui.dll is checked against the build it was shipped as before it
;  is loaded, as dcx.dll is: a file that is not that one is not run.
;  dccore_icons.dll holds pictures only and is never run.

alias dccore.theme.dir { return $scriptdir $+ dll\ }
alias dccore.theme.uifile { return $dccore.theme.dir $+ dccore_ui.dll }
alias dccore.theme.iconfile { return $dccore.theme.dir $+ dccore_icons.dll }
alias dccore.theme.uisha256 { return 3927391c2c3bfd1ed7a64cdbd806c45b758dce53665d6c7bcde54ae276d14e1c }

alias dccore.theme.mode {
  var %m = $dccore.opt(theme)
  return $iif($istok(dark light,%m,32),%m,auto)
}
; mIRC's own dark mode. $darkmode is mIRC 7.79's; before it, no dark mode.
; Asked as a condition, not compared with $true, so it is right whether it
; answers $true or 1.
alias dccore.theme.mircdark {
  if ($version < 7.79) { return $false }
  return $iif($darkmode,$true,$false)
  :error
  reseterror
  return $false
}
alias dccore.theme.dark {
  var %m = $dccore.theme.mode
  if (%m == dark) { return $true }
  if (%m == light) { return $false }
  return $dccore.theme.mircdark
}
; Why dll\dccore_ui.dll is not used, or nothing when it is ($sha256 needs
; mIRC 7.45, as DCX does; the dll is 32-bit, as dcx.dll is). Its hash is
; worked out again for as long as mIRC does not hold the dll - a file put
; right is taken without a restart, a file swapped is caught before it is
; loaded - and kept once mIRC holds it, which a file changed on disk cannot
; change.
alias dccore.theme.uiwhy {
  if (!$isfile($dccore.theme.uifile)) { return dll\dccore_ui.dll is missing }
  if ($version < 7.45) { return it needs mIRC 7.45 or later }
  if ($bits == 64) { return this mIRC is 64-bit and dll\dccore_ui.dll is 32-bit }
  if ($dccore.st(ui.sha) == $null) || (!$dccore.theme.uiloaded) { hadd dccore.live ui.sha $sha256($dccore.theme.uifile,2) }
  if ($dccore.st(ui.sha) != $dccore.theme.uisha256) { return dll\dccore_ui.dll is not the build this script was shipped with }
}
alias dccore.theme.ui { return $iif($dccore.theme.uiwhy == $null,$true,$false) }
; Every call into dll\dccore_ui.dll goes through here: $1 the function, $2-
; what it is given. Nothing unless the file is the build the script pins,
; asked here and not by each caller, so no call can come before the check.
; A dll that cannot be opened all the same is an error that would stop the
; script, so it is caught, as dccore.dcx.version does.
alias dccore.theme.call {
  if (!$dccore.theme.ui) { return }
  return $dll($dccore.theme.uifile,$1,$2-)
  :error
  reseterror
  return
}
; Whether the dialogs are drawn dark: the theme is dark and something draws
; them so - mIRC's own dark mode, or dccore_ui.dll. Without either they stay
; light, the control panel's DCX parts too, so nothing is half dark.
alias dccore.theme.dialogsdark {
  if (!$dccore.theme.dark) { return $false }
  if ($dccore.theme.mircdark) { return $true }
  return $dccore.theme.ui
}
; The icons option: on unless it is set to 0, so a script reloaded over an
; older one (/reload -rs runs neither on LOAD nor on START, and so sets no
; default) has them as a fresh one does.
alias dccore.theme.icons { return $iif($dccore.opt(icons) == 0,$false,$true) }

; The colours of dark, as $rgb gives them: bg the dialogs, face the DCX
; frames and the tab chosen, light the pills and the other tabs, line the
; DCX lines, panel the lists, fg the text, grey the quieter text.
alias dccore.theme.rgb {
  if ($1 == bg) || ($1 == face) { return $rgb(32,32,32) }
  if ($1 == light) { return $rgb(45,45,48) }
  if ($1 == line) { return $rgb(85,85,85) }
  if ($1 == panel) { return $rgb(43,45,48) }
  if ($1 == fg) { return $rgb(238,238,238) }
  if ($1 == grey) { return $rgb(150,150,150) }
}
; The control panel's DCX colours (#1288): dark, the theme's; light, the
; system's, exactly what the window always used. Dark only where its
; dialogs are drawn dark too (dccore.theme.dialogsdark).
alias dccore.theme.sys {
  if ($dccore.theme.dialogsdark) { return $dccore.theme.rgb($1) }
  if ($1 == line) { return $dccore.dcx(GetSystemColor,COLOR_3DSHADOW) }
  if ($1 == face) { return $dccore.dcx(GetSystemColor,COLOR_3DFACE) }
  if ($1 == light) { return $dccore.dcx(GetSystemColor,COLOR_3DLIGHT) }
  return $dccore.dcx(GetSystemColor,COLOR_GRAYTEXT)
}
; $1 a dialog: dark or back to the system's look, through dccore_ui.dll.
; $2 init: the dialog is just made, and only a dark one needs anything.
; The control panel is "x" when DCX has marked it, the DCX check always.
alias dccore.theme.dialog {
  var %d = $1
  if (!$dialog(%d)) { return }
  if ($dccore.theme.mircdark) { return }
  if ($2 == init) && (!$dccore.theme.dark) { return }
  ; light: only through a dll that has darkened something, never by loading
  ; one to draw a dialog the way it already is
  if (!$dccore.theme.dark) && (!$dccore.theme.uiloaded) { return }
  var %x = -
  if (%d == dccore.dcxcheck) { %x = x }
  elseif (%d == dccore.set) && ($hget(dccore.sws,dcx)) { %x = x }
  noop $dccore.theme.call(ApplyTheme,$dialog(%d).hwnd $iif($dccore.theme.dark,1,0) %x)
}

; ---- colours in the windows ------------------------------------------

; The background a colour is drawn on, as a palette number: $1 win for a
; window's text, list for a side listbox. The window's own Background from
; the mIRC settings first, then mIRC's dark mode (black), then mIRC's own
; colour for it.
alias dccore.theme.bgidx {
  if ($1 != list) && ($dccore.opt(bg) isnum 0-15) { return $dccore.opt(bg) }
  if ($dccore.theme.mircdark) { return 1 }
  var %c = $color($iif($1 == list,listbox,background))
  return $iif(%c isnum 0-15,%c,0)
}
; $1 a colour number, $2 win or list: the same colour, or on a dark
; background (black, navy, maroon, purple) the light neighbour of one that
; would not be seen there. Anything that is not a colour number - -1,
; "name", "per" - and everything in the light theme, unchanged.
alias dccore.theme.legible {
  if ($1 !isnum 0-15) { return $1 }
  if ($dccore.theme.mode == light) { return $1 }
  if (!$istok(1 2 5 6,$int($dccore.theme.bgidx($2)),32)) { return $1 }
  var %at = $findtok(1 2 3 5 6 12,$int($1),1,32)
  return $iif(%at,$gettok(0 11 9 4 13 11,%at,32),$1)
}
; A colour from the mIRC settings for a side listbox line (the panel, the
; Downloads list)
alias dccore.lcol { return $dccore.theme.legible($dccore.opt(col. $+ $1),list) }

; ---- menus -----------------------------------------------------------

; The icons in dll\dccore_icons.dll, in its order: the Nth name is icon N-1
; in the file, N in a menu's or a listview's image list.
alias dccore.ico.names { return app console settings script status slots queue dlqueue uptime version help lists fetch ask purge library rebuild audio ban unban clear ignore moveup movedown update rehash stop connect disconnect pair unpair trust chat downloads panel font dashboard cancel again user speed search pack folder hash palette megaphone mail bug key home shield }
; $1 an icon's name: what goes in front of a menu item's text
alias dccore.mi {
  ; a script reloaded with /reload -rs ran no on START: the first menu
  ; drawn starts the hook, once
  if (!$dccore.st(mi.tried)) { dccore.theme.menus }
  if (!$dccore.st(mi.on)) { return }
  var %n = $findtok($dccore.ico.names,$1,1,32)
  if (%n) { return %n $+ $chr(11) }
}

; The menu icons on or off, as /dccore icons says, through dccore_ui.dll's
; hook (MenuIcons).
alias dccore.theme.menus {
  hadd dccore.live mi.tried 1
  var %on = 0
  if ($dccore.theme.icons) && ($isfile($dccore.theme.iconfile)) { %on = 1 }
  if (%on) && ($dccore.theme.ui) {
    hadd dccore.live mi.on $iif($dccore.theme.call(MenuIcons,$dccore.theme.iconfile) == OK,1,0)
    return
  }
  dccore.theme.menusoff
}
; the hook off - only through a dccore_ui.dll this script has loaded already,
; never by loading it for that
alias dccore.theme.menusoff {
  if ($dccore.st(mi.on)) && ($dccore.theme.uiloaded) { noop $dccore.theme.call(MenuIcons,off) }
  hdel dccore.live mi.on
}
; Whether mIRC holds dll\dccore_ui.dll loaded now: $dll(N) names the Nth
; dll it has loaded, $dll(0) how many (the dll asks to be kept, mKeep, so
; mIRC does not drop it after ten idle minutes as it does others)
alias dccore.theme.uiloaded {
  var %i = 1
  while (%i <= $dll(0)) {
    if ($nopath($dll(%i)) == dccore_ui.dll) { return $true }
    inc %i
  }
  return $false
}

; ---- the control panel's DCX parts ------------------------------------

; Just after the control panel is made (its init): the page list's icons,
; and in the dark theme the background, the texts and the lists. Each in a
; timer of its own: a DCX command that fails halts what called it, and so
; it halts only that part, never the window being made.
alias dccore.theme.dcxset {
  if (!$hget(dccore.sws,dcx)) { return }
  if ($dccore.theme.icons) && ($isfile($dccore.theme.iconfile)) { .timerdccoreThemeIcons -m 1 0 dccore.theme.pageicons }
  if (!$dccore.theme.dialogsdark) { return }
  .timerdccoreThemeBg -m 1 0 dccore.theme.dcxbg
  .timerdccoreThemeText -m 1 0 dccore.theme.dcxtexts
  .timerdccoreThemeLists -m 1 0 dccore.theme.dcxlists
}
alias dccore.theme.dcxbg { if ($dialog(dccore.set)) { dccore.dcx xdialog dccore.set -g +b $dccore.theme.rgb(bg) } }
; $1 a DCX text, $2 1 when it has no background of its own: light text, on
; the face
alias dccore.theme.dcxtext {
  dccore.dcx xdid dccore.set $1 -C +t $dccore.theme.rgb(fg)
  if ($2) { dccore.dcx xdid dccore.set $1 -C +b $dccore.theme.rgb(face) }
}
; Every DCX text made at open, from the layout's own ids (dccore.sw.data).
; The quieter texts keep the grey dccore.sw.dcxhome and dcxstats gave them.
alias dccore.theme.dcxtexts {
  if (!$dialog(dccore.set)) || (!$hget(dccore.swm)) { return }
  ; the tabs' names: the third control of each tab
  var %t = $dccore.sw.m(dcxtabs), %i = 1
  while (%i <= $gettok(%t,6,32)) {
    dccore.theme.dcxtext $calc($gettok(%t,1,32) + $gettok(%t,2,32) * (%i - 1) + 2)
    inc %i
  }
  ; the page's name and the side's name, over the window's background
  dccore.theme.dcxtext $gettok($dccore.sw.m(dcxtitle),1,32) 1
  dccore.theme.dcxtext $gettok($dccore.sw.m(dcxcrumb),1,32) 1
  ; the frames' titles take theirs as a page's frames are made, the first
  ; time it is shown (dccore.sw.dcxframes)
  ; the pills' names, and the arrow of the way back
  %i = 1
  while (%i <= $dccore.sw.m(home.n)) {
    dccore.theme.dcxtext $calc($gettok($dccore.sw.m(home. $+ %i),1,32) + 2)
    inc %i
  }
  dccore.theme.dcxtext $calc($gettok($dccore.sw.m(dcxback),1,32) + 2)
  ; the start page's figures: each frame's title, each value (its key stays
  ; grey) or each line of fixed-width text
  %i = 1
  while (%i <= $dccore.sw.m(home.f.n)) {
    dccore.theme.dcxtext $gettok($dccore.sw.m(home.f. $+ %i),2,32)
    var %j = 1
    while (%j <= $dccore.sw.m($+(home.f.,%i,.n))) {
      var %r = $dccore.sw.m($+(home.f.,%i,.r.,%j))
      if ($gettok(%r,2,32)) {
        dccore.dcx xdid dccore.set $gettok(%r,1,32) -C +b $dccore.theme.rgb(face)
        dccore.theme.dcxtext $gettok(%r,2,32) 1
      }
      else { dccore.theme.dcxtext $gettok(%r,1,32) 1 }
      inc %j
    }
    inc %i
  }
  ; the Connection row keeps its state's colour (dccore.sw.home.ink)
  var %ink = $dccore.sw.s(home.ink)
  if (%ink != $null) { dccore.dcx xdid dccore.set $gettok(%ink,1,32) -C +t $gettok(%ink,2,32) }
}
; The page list's colours. The tables get theirs as they are made, the
; first time they are needed (dccore.sw.lv.make) - enabled, as
; dccore.sw.enable keeps them in the dark theme.
alias dccore.theme.dcxlists {
  if (!$dialog(dccore.set)) || (!$hget(dccore.swm)) { return }
  dccore.theme.dcxlist $gettok($dccore.sw.m(dcxpages),1,32)
}
; $1 a DCX listview: light text on the lists' colour - a listview's colours
; are -i, not -C
alias dccore.theme.dcxlist {
  dccore.dcx xdid dccore.set $1 -i +bk $dccore.theme.rgb(panel)
  dccore.dcx xdid dccore.set $1 -i +t $dccore.theme.rgb(fg)
}
; The page list's icons: the whole file into its image list, so a page's
; icon is its number among dccore.ico.names - then its rows again, now
; with their pictures, as dccore.sw.tab fills them
alias dccore.theme.pageicons {
  if (!$dialog(dccore.set)) || (!$hget(dccore.swm)) { return }
  dccore.dcx xdid dccore.set $gettok($dccore.sw.m(dcxpages),1,32) -w +n -1 $dccore.theme.iconfile
  hadd dccore.sws theme.icons 1
  var %g = $dccore.sw.s(tab)
  if (!%g) { return }
  dccore.sw.dcxpages $dccore.sw.m(g. $+ %g $+ .pages)
  if ($dccore.sw.s(page)) { dccore.sw.dcxshow $dccore.sw.s(page) }
}
; $1 a page's number: its icon in the page list, 0 for none - a page the
; layout gains later simply has none until it is named here
alias dccore.theme.pageicon {
  if (!$dccore.sw.s(theme.icons)) { return 0 }
  var %pair = $wildtok($dccore.theme.pagemap,$dccore.sw.untext($dccore.sw.m(p. $+ $1)) $+ =*,1,44)
  if (%pair == $null) { return 0 }
  return $findtok($dccore.ico.names,$gettok(%pair,2,61),1,32)
}
alias dccore.theme.pagemap { return IRC Server=connect,General Settings=settings,Channels=hash,Operator=user,Appearance=palette,Advertising=megaphone,Lists=lists,Channel bindings=pair,List settings=library,Rebuild=rebuild,Transfers=slots,Search=search,List discovery=fetch,Fetch tuning=speed,Queue=queue,Anti-flood=shield,Bans & ignores=ban,Private messages=mail,Admin console=key,Web dashboard=dashboard,Console feed=status,Debug & logging=bug,File locations=folder,@DCCore window=console,Window=panel,Connection=pair,Chat=chat }

; ---- start, stop, change ---------------------------------------------

alias dccore.theme.start { dccore.theme.menus }
alias dccore.theme.stop { dccore.theme.menusoff }
; After /dccore theme or icons: what is open drawn again. The control panel
; is drawn once, when it opens, so it takes a change the next time.
alias dccore.theme.refresh {
  dccore.theme.menus
  var %w = dccore.dq dccore.setl dccore.dcxcheck, %i = 1
  while (%i <= $numtok(%w,32)) {
    if ($dialog($gettok(%w,%i,32))) { dccore.theme.dialog $gettok(%w,%i,32) }
    inc %i
  }
  if ($dialog(dccore.set)) { dccore.sys The control panel takes the change the next time it opens. }
  if ($window($dccore.win)) { dccore.panel }
  if ($window($dccore.dl.win)) { dccore.dl.draw }
}
alias dccore.theme.status {
  var %ui = $dccore.theme.uiwhy
  dccore.sys Theme: $dccore.theme.mode $+ , now $iif($dccore.theme.dark,dark,light) $+ $iif($dccore.theme.mircdark,$chr(44) as mIRC's own dark mode) $+ . $iif(($dccore.theme.dark) && (!$dccore.theme.mircdark) && (%ui != $null),The dialogs stay light: %ui $+ .) $iif(($dccore.theme.dark) && (!$dccore.theme.mircdark),The menus follow mIRC: its own dark mode follows Windows in mIRC 7.79 and later and is under Options - Display in mIRC 7.81 and later.) /dccore theme auto|dark|light
  if (!$dccore.theme.icons) { dccore.sys Icons: off. /dccore icons on | return }
  if (!$isfile($dccore.theme.iconfile)) { dccore.sys Icons: dll\dccore_icons.dll is missing. | return }
  dccore.sys Icons: $iif($dccore.st(mi.on),on in this script's menus,not in the menus - $iif(%ui != $null,%ui,the menu hook would not start)) $+ ; the control panel's page list has them when DCX draws it. /dccore icons off
}

; ---------------------------------------------------------------------
;  This mIRC's own settings: the control panel's mIRC side (#1288)
; ---------------------------------------------------------------------
;
;  What @DCCore shows and in which colours, the window's look, the bot
;  this mIRC is paired with, DCCore Chat - kept in dccore.ini beside the
;  script, never on the bot. They were the Options dialog; they are pages
;  of the control panel now, in its form - its mIRC side, the mIRC tab
;  of the native form - and /dccore options opens the window there. The
;  checks among them are the window's locals (dccore.sw.savelocal); the
;  menus, the fields, the pairing line and the console feed check are
;  filled by dccore.sw.opt.fill and read back by dccore.sw.opt.save,
;  which Apply runs and which redraws what changed. The console feed
;  check is the bot's own live state, never saved here.

alias dccore.options { dccore.settings mirc }

alias dccore.colours { return 00 white,01 black,02 navy,03 green,04 red,05 maroon,06 purple,07 orange,08 yellow,09 lime,10 teal,11 cyan,12 blue,13 pink,14 grey,15 silver }
alias dccore.groups { return request queued sends fail search joins bans info }

; The Script pages filled from dccore.ini: the line types' checks and
; colours (1620 up, a check and a menu each - a menu's lines are added when
; its page is first shown, dccore.sw.cb.fill), the colours of file names,
; console replies, panel headings, search text, nicks and channels (1640
; to 1645), the status line's minutes (1637), the font (1670, 1671), the
; background (1673), the finished downloads shown (1675), the bot's nick
; and pairing (1681, 1682), the console feed (1685).
alias dccore.sw.opt.fill {
  var %i = 1
  while (%i <= 8) {
    var %g = $gettok($dccore.groups,%i,32)
    if ($dccore.opt(show. $+ %g)) { did -c dccore.set $calc(1618 + 2 * %i) }
    dccore.fillcombo $calc(1619 + 2 * %i) $dccore.opt(col. $+ %g)
    inc %i
  }
  dccore.fillcombo 1640 $dccore.opt(col.name)
  dccore.fillcombo 1641 $dccore.opt(col.console)
  dccore.fillcombo 1642 $dccore.opt(col.head)
  dccore.fillspan 1643 $dccore.opt(col.term) name -1
  dccore.fillspan 1644 $dccore.opt(col.nick) -1 per
  dccore.fillspan 1645 $dccore.opt(col.chan) -1
  did -ra dccore.set 1637 $dccore.opt(statusmin)
  if ($dccore.opt(font)) { did -c dccore.set 1670 }
  did -ra dccore.set 1671 $dccore.fontsize
  dccore.fillbg 1673
  did -ra dccore.set 1675 $dccore.dl.rows
  ; the theme (#1368): auto, dark, light on lines 1 to 3, and the icons
  did -r dccore.set 1691
  did -a dccore.set 1691 Follow mIRC
  did -a dccore.set 1691 Dark
  did -a dccore.set 1691 Light
  did -c dccore.set 1691 $findtok(auto dark light,$dccore.theme.mode,1,32)
  if ($dccore.theme.icons) { did -c dccore.set 1692 }
  if ($dccore.bot) { did -ra dccore.set 1681 $dccore.bot }
  did -ra dccore.set 1682 $iif($dccore.opt(token),Paired $dccore.opt(paired) (token in dccore.ini),Not paired: the bot will ask for the password)
  ; The bot's own setting, not a local pref (#572 follow-up): nothing is
  ; saved for it in dccore.ini, it lives only in dccore.live, refreshed
  ; every time the bot says HELLO. Unknown (never told yet - a window
  ; opened in the instant after connecting) leaves it unchecked rather
  ; than guessing either way (#1006 follow-up).
  if ($dccore.st(consolefeed) == on) { did -c dccore.set 1685 }
  hadd dccore.sws opt.sig $dccore.sw.opt.sig
}
; Every one of those controls as it is now, in one line: what "changed"
; means for them (the checks among the locals have their own test)
alias dccore.sw.opt.sig {
  var %i = 1, %s
  while (%i <= 8) { %s = %s $did(dccore.set,$calc(1618 + 2 * %i)).state $dccore.sw.cb.sel($calc(1619 + 2 * %i)) | inc %i }
  %i = 1640
  while (%i <= 1645) { %s = %s $dccore.sw.cb.sel(%i) | inc %i }
  return %s $did(dccore.set,1637).text $did(dccore.set,1670).state $did(dccore.set,1671).text $dccore.sw.cb.sel(1673) $did(dccore.set,1675).text $did(dccore.set,1681).text $did(dccore.set,1685).state $did(dccore.set,1691).sel $did(dccore.set,1692).state
}
alias dccore.sw.opt.dirty { return $iif($dccore.sw.opt.sig === $dccore.sw.s(opt.sig),$false,$true) }
; Apply: this mIRC's switches (the locals) and the pages above into
; dccore.ini, and the window redrawn where that changes it. How many
; switches changed.
alias dccore.sw.opt.save {
  var %panel = $dccore.opt(panel), %bg = $dccore.opt(bg), %dl = $dccore.dl.rows, %n = $dccore.sw.savelocal, %i = 1
  while (%i <= 8) {
    var %g = $gettok($dccore.groups,%i,32)
    hadd dccore show. $+ %g $did(dccore.set,$calc(1618 + 2 * %i)).state
    hadd dccore col. $+ %g $calc($dccore.sw.cb.sel($calc(1619 + 2 * %i)) - 1)
    inc %i
  }
  hadd dccore col.name $calc($dccore.sw.cb.sel(1640) - 1)
  hadd dccore col.console $calc($dccore.sw.cb.sel(1641) - 1)
  hadd dccore col.head $calc($dccore.sw.cb.sel(1642) - 1)
  hadd dccore col.term $dccore.spanval(1643,name -1)
  hadd dccore col.nick $dccore.spanval(1644,-1 per)
  hadd dccore col.chan $dccore.spanval(1645,-1)
  hadd dccore statusmin $iif($did(dccore.set,1637).text isnum,$int($did(dccore.set,1637).text),5)
  hadd dccore font $did(dccore.set,1670).state
  if ($did(dccore.set,1671).text isnum) && ($did(dccore.set,1671).text >= 6) { hadd dccore fontsize $did(dccore.set,1671).text }
  hadd dccore bg $calc($dccore.sw.cb.sel(1673) - 2)
  hadd dccore dlfinished $iif($did(dccore.set,1675).text isnum 1-15,$int($did(dccore.set,1675).text),15)
  if ($did(dccore.set,1681).text != $null) { hadd dccore bot $did(dccore.set,1681).text }
  ; the theme and the icons (#1368): saved as /dccore theme and /dccore
  ; icons save them, and what is open drawn again only when one changed
  var %theme = $gettok(auto dark light,$did(dccore.set,1691).sel,32), %icons = $iif($did(dccore.set,1692).state == 1,1,0), %retheme = 0
  if (%theme != $null) && (%theme != $dccore.theme.mode) { hadd dccore theme %theme | %retheme = 1 }
  if (%icons != $iif($dccore.theme.icons,1,0)) { hadd dccore icons %icons | %retheme = 1 }
  dccore.save
  ; consolefeed is the bot's own (#1006 follow-up): sent only when the
  ; check disagrees with what the bot last told us, so an Apply that did
  ; not touch it does not rehash the bot - and only once the bot HAS told
  ; us: before its first line the state is empty, the box shows unticked,
  ; and empty never equals "off"
  var %consolefeed = $iif($did(dccore.set,1685).state == 1,on,off)
  if ($dccore.st(consolefeed) != $null && %consolefeed != $dccore.st(consolefeed)) { dccore.send consolefeed %consolefeed }
  if ($window($dccore.dl.win)) && (%dl != $dccore.dl.rows) { dccore.dl.tell }
  if ($window($dccore.win)) {
    if (%panel != $dccore.opt(panel)) { dccore.rebuild }
    if ($dccore.opt(font)) { font $dccore.win $dccore.fontsize Lucida Console }
    if (%bg != $dccore.opt(bg)) { dccore.background }
    dccore.title
    dccore.panel
  }
  if (%retheme) { dccore.theme.refresh }
  hadd dccore.sws opt.sig $dccore.sw.opt.sig
  return %n
}
; Pair again, Forget token: the window closes and the pairing takes over
alias dccore.sw.opt.pair {
  if ($did(dccore.set,1681).text != $null) { dccore.set bot $did(dccore.set,1681).text }
  dialog -x dccore.set
  dccore pair
}
alias dccore.sw.opt.unpair {
  dialog -x dccore.set
  dccore unpair
}
; These three set a menu up: its lines are added the first time its page
; is shown (dccore.sw.cb.fill, recipe bg, colours or span), its selection
; is kept till then (dccore.sw.cb.select).
; $1 a menu: "none" first, then the sixteen colours - the selected line is the colour + 2
alias dccore.fillbg {
  dccore.sw.cb.lazy $1 bg
  dccore.sw.cb.select $1 $calc($dccore.opt(bg) + 2)
}
; $1 a menu, $2 the saved colour: the sixteen colours, colour C on line C + 1
alias dccore.fillcombo {
  dccore.sw.cb.lazy $1 colours
  dccore.sw.cb.select $1 $calc($2 + 1)
}
; The nick, channel and search text menus (#1259): their own lines first,
; then the sixteen colours. $1 the menu, $2 the saved value, $3- the values
; of the lines before the colours, in order (-1 "same as the line", per
; "per nick", name "same as File names"). With N such lines, colour C is
; line C + N + 1.
alias dccore.fillspan {
  var %own = $3-, %n = $numtok(%own,32)
  dccore.sw.cb.lazy $1 span %own
  var %at = $findtok(%own,$2,1,32)
  if (!%at) { %at = $iif($2 isnum 0-15,$calc($2 + %n + 1),1) }
  dccore.sw.cb.select $1 %at
}
alias dccore.spanlabel {
  if ($1 == name) { return same as File names }
  if ($1 == per) { return per nick }
  return same as the line
}
; What the selected line of such a menu means: $1 the menu, $2 the same
; values dccore.fillspan was given, as one argument.
alias dccore.spanval {
  var %sel = $dccore.sw.cb.sel($1), %n = $numtok($2,32)
  if (%sel <= %n) { return $gettok($2,$iif(%sel < 1,1,%sel),32) }
  return $calc(%sel - %n - 1)
}


; ---------------------------------------------------------------------
;  Settings window (#1264): the bot's settings, as its dashboard has them
; ---------------------------------------------------------------------
;
;  /dccore settings, or Settings... in the menus. Six tabs along the top,
;  the pages of the chosen tab in a column of buttons on the left, Apply / OK / Cancel
;  at the bottom. Everything goes through the console (docs/ADMIN-CONSOLE.md,
;  "Settings over the console"), so the bot checks and saves a value
;  exactly as its dashboard's Settings page does, and says why it refused
;  one in the page's own words.
;
;  The dialog table and its lookup data are GENERATED: the block between
;  the BEGIN and END lines below is written by
;  scripts/mirc/build_settings_window.py from the bot's own settings
;  metadata and scripts/mirc/settings_window_layout.py. Change those, not
;  the block. What follows the block is written by hand: it opens, loads,
;  keeps track of what changed and saves.
;
;  The tabs are only a strip. No control is attached to one - mIRC would
;  show every control of a tab again whenever it is clicked - so a page's
;  controls are shown, and the page before hidden, by hand (did -v/-h).
;
;  dccore.swm   the generated lookup data (dccore.sw.data): k.<KEY> is
;               "<control> <kind> <unit factor> <label>", p.<page>.<n> the
;               page's control ids, h.<KEY> the help, i.<id> the setting a
;               control belongs to, ...
;  dccore.sws   this opening of the window: the values as loaded (v.<KEY>,
;               "=" and the value), what each control showed then (d.<KEY>),
;               the structured pages' rows, and where a transaction is
;               (phase: caps, load, idle, apply, confirm, aborting, preview).
;
;  Every snapshot the bot sends is counted against its BEGIN and END lines
;  before anything is shown from it: the bot's outbox drops lines for a
;  client that falls behind, and a page edited from half a snapshot would
;  save half a configuration.
;
;  It needs mIRC 6.17 or later, for $regsubex, which decodes the values.

; The control panel: with DCX it opens on its start page, Bot or mIRC to
; choose from; the native form on its first tab. $1 mirc: opened on this
; mIRC's own settings - the mIRC side, the mIRC tab
alias dccore.settings {
  dccore.init
  if ($version < 6.17) { dccore.sys The control panel needs mIRC 6.17 or later. The console has the same: type settings in this window. | return }
  if ($dialog(dccore.set)) {
    dialog -v dccore.set
    if ($1 == mirc) { dccore.sw.go $iif($dccore.sw.s(dcx),side 2,tab $dccore.sw.m(groups)) }
    return
  }
  hadd dccore.live sw.open $iif($1 == mirc,mirc,start)
  dialog -m dccore.set dccore.set
}
; $1 a tab's name: its number, or 1
alias dccore.sw.group {
  var %g = 1
  while (%g <= $dccore.sw.m(groups)) {
    if ($dccore.sw.m(g. $+ %g) == $1) { return %g }
    inc %g
  }
  return 1
}

; ---- DCX (#1288) -------------------------------------------------------
;  The settings window is to be drawn with DCX (scripts/mirc/dcx/dcx.dll,
;  BSD-3-Clause - see the README beside it) when it can be loaded, and with
;  mIRC's own controls when it cannot. $dccore.dcx.why says why not, or
;  nothing when DCX can be used; /dccore dcx shows it.
;
;  The dll is checked against the release it was shipped as before it is
;  loaded: a file that is not that one is not run. One already loaded by
;  another script is used as it is - it is in this mIRC already, and a second
;  copy of dcx.dll beside it is what DCX itself warns against.

alias dccore.dcx.sha256 { return 321e9ac2168734edd6ff6e48f4bd9540e9297f5e1360773edda9d554e620db12 }
; Microsoft's WebView2 loader, shipped beside it: DCX loads it every time it
; starts (from mIRC's folder, then from beside dcx.dll) and writes a warning
; to the status window when it is not there. Missing is harmless; a file
; that is not the shipped one is not let near the dll.
alias dccore.dcx.wv2sha256 { return 5659cbae9f3d412662515671a6c85aefe08eee17118c3de1330a2fed74dc415f }
alias dccore.dcx.file { return $scriptdir $+ dcx\dcx.dll }
alias dccore.dcx.wv2file { return $scriptdir $+ dcx\WebView2Loader.dll }
alias dccore.dcx.loaded { return $dll(dcx.dll) }
alias dccore.dcx.path { return $iif($dccore.dcx.loaded,$v1,$dccore.dcx.file) }
alias dccore.dcx.why {
  if ($dccore.opt(nodcx)) { return the option to use the built-in window is on }
  if ($version < 7.45) { return DCX needs mIRC 7.45 or later }
  if ($bits == 64) { return this mIRC is 64-bit and dcx.dll is 32-bit }
  if (!$dccore.dcx.loaded) {
    if (!$isfile($dccore.dcx.file)) { return $dccore.dcx.file is missing }
    if ($sha256($dccore.dcx.file,2) != $dccore.dcx.sha256) { return $dccore.dcx.file is not the release this script was shipped with }
    if ($isfile($dccore.dcx.wv2file)) && ($sha256($dccore.dcx.wv2file,2) != $dccore.dcx.wv2sha256) { return $dccore.dcx.wv2file is not the one this script was shipped with }
  }
  if ($dccore.dcx.version == $null) { return dcx.dll would not load }
}
; The dll's own version line, or nothing when it does not load. A dll that
; cannot be opened is an error that would stop the script, so it is caught.
alias dccore.dcx.version {
  return $dll($dccore.dcx.path,Version,$null)
  :error
  reseterror
  return
}
; DCX itself: /dccore.dcx <function> <args>, or $dccore.dcx(<function>,<args>).
; Its xdialog and xdid take the dialog first: /dccore.dcx xdialog <dialog> -c ...
alias dccore.dcx {
  if ($isid) { return $dll($dccore.dcx.path,$1,$2-) }
  dll $qt($dccore.dcx.path) $1 $2-
}
alias dccore.dcx.status {
  var %why = $dccore.dcx.why
  if (%why == $null) { dccore.sys DCX: $dccore.dcx.version from $dccore.dcx.path $+ . /dccore dcx check opens a small window that tests it in this mIRC. }
  else { dccore.sys DCX is not used: %why $+ . The settings window uses mIRC's own controls. }
}

; /dccore dcx check: what can only be learnt in a real mIRC. Whether mIRC's
; own controls still raise their events once DCX has marked the dialog, and
; how DCX's pixels line up with this dialog's units: the tree is placed by
; the scale $dialog().cw reports, so it fills the box exactly when that
; scale is right and sits small in its corner when it is not.
dialog dccore.dcxcheck {
  title "DCCore - DCX check"
  size -1 -1 240 120
  option dbu
  button "Click this (mIRC's own button)", 1, 4 4 100 13
  box "The DCX tree should fill this box", 2, 110 4 126 90
  text "Click the button and an item in the tree, then read @DCCore.", 3, 4 22 100 40
  button "Close", 4, 190 102 46 13, cancel
}
on *:dialog:dccore.dcxcheck:init:0: { dccore.dcx.checkinit | dccore.theme.dialog dccore.dcxcheck init }
on *:dialog:dccore.dcxcheck:sclick:1: { dccore.sys DCX check: mIRC's own button raised its click in the DCX-marked window. }
alias dccore.dcx.check {
  var %why = $dccore.dcx.why
  if (%why != $null) { dccore.sys DCX check skipped: %why $+ . | return }
  if ($dialog(dccore.dcxcheck)) { dialog -v dccore.dcxcheck | return }
  dialog -m dccore.dcxcheck dccore.dcxcheck
}
alias dccore.dcx.checkinit {
  dccore.dcx Mark dccore.dcxcheck dccore.dcx.checkcb
  var %cw = $dialog(dccore.dcxcheck).cw, %ch = $dialog(dccore.dcxcheck).ch
  var %sx = $calc(%cw / 240), %sy = $calc(%ch / 120)
  dccore.sys DCX check: $dccore.dcx.version $+ . The 240 x 120 unit window reports a client area of %cw x %ch $+ , a scale of %sx x %sy $+ .
  var %x = $round($calc(114 * %sx),0), %y = $round($calc(14 * %sy),0), %w = $round($calc(118 * %sx),0), %h = $round($calc(76 * %sy),0)
  dccore.dcx xdialog dccore.dcxcheck -c 10 treeview %x %y %w %h hasbuttons haslines linesatroot showsel
  dccore.dcx xdid dccore.dcxcheck 10 -a $+(-1,$chr(9),+eE 0 0 0 0 0 0 0 0 General)
  dccore.dcx xdid dccore.dcxcheck 10 -a $+(1 -1,$chr(9),+E 0 0 0 0 0 0 0 0 IRC Server)
  dccore.dcx xdid dccore.dcxcheck 10 -a $+(1 -1,$chr(9),+E 0 0 0 0 0 0 0 0 Channels)
  dccore.dcx xdid dccore.dcxcheck 10 -a $+(-1,$chr(9),+eE 0 0 0 0 0 0 0 0 Sharing)
  dccore.dcx xdid dccore.dcxcheck 10 -a $+(2 -1,$chr(9),+E 0 0 0 0 0 0 0 0 Search)
}
; DCX's events: $1 the dialog, $2 the event, $3 the control, $4- its details
alias dccore.dcx.checkcb {
  if ($2 == sclick) && ($3 == 10) { dccore.sys DCX check: the tree raised its click (item $4- $+ ). }
}

; The lines this window reads, routed here by dccore.structured while it is
; open (and for a few seconds after, so a late reply is not shown as noise).
alias dccore.sw.types { return CAPS SETBEGIN SETF SETEND SETOPEN SETERR SETDONE SETAPPLIED LOCKED UNLOCKED UNLOCK PVBEGIN PVLINE PVEND SRVBEGIN SRVLIST SRVCHAN SRVFOLDER SRVEND SRVOPEN SRVERR SRVDONE FLDBEGIN FLDROW FLDEND FLDOPEN FLDERR FLDDONE OCBEGIN OCLINE OCEND OCOPEN OCERR OCDONE OCRESEND BANBEGIN BANP BANT BANEND }

; Whether the window is waiting for a line of type $1: its phase, or the
; wait (w.<what>) or flag (saving.srv, saving.oc, resending.oc, unlocking,
; applying) of the request it answers. Anything else is somebody typing in
; @DCCore and is shown there.
alias dccore.sw.wants {
  if (!$dialog(dccore.set)) || (!$hget(dccore.sws)) { return $false }
  var %t = $1, %phase = $dccore.sw.s(phase)
  if (%t == CAPS) { return $iif(%phase == caps,$true,$false) }
  if ($istok(SETBEGIN SETF SETEND,%t,32)) { return $iif(%phase == load,$true,$false) }
  if ($istok(SETOPEN SETERR SETDONE,%t,32)) { return $iif($istok(apply confirm aborting preview locked,%phase,32),$true,$false) }
  if (%t == SETAPPLIED) { return $iif($dccore.sw.s(applying) != $null,$true,$false) }
  if ($istok(PVBEGIN PVLINE PVEND,%t,32)) { return $iif(%phase == preview,$true,$false) }
  if ($istok(SRVBEGIN SRVLIST SRVCHAN SRVFOLDER SRVEND,%t,32)) { return $iif($dccore.sw.s(w.served) != $null,$true,$false) }
  if ($istok(FLDBEGIN FLDROW FLDEND,%t,32)) { return $iif($dccore.sw.s(w.folders) != $null,$true,$false) }
  if ($istok(SRVOPEN SRVERR SRVDONE FLDOPEN FLDERR FLDDONE,%t,32)) { return $iif($dccore.sw.s(saving.srv),$true,$false) }
  if ($istok(OCBEGIN OCLINE OCEND,%t,32)) { return $iif($dccore.sw.s(w.onconnect) != $null,$true,$false) }
  if ($istok(OCOPEN OCERR OCDONE,%t,32)) { return $iif($dccore.sw.s(saving.oc),$true,$false) }
  if (%t == OCRESEND) { return $iif($dccore.sw.s(resending.oc),$true,$false) }
  if ($istok(BANBEGIN BANP BANT BANEND,%t,32)) { return $iif($dccore.sw.s(w.banlist) != $null,$true,$false) }
  if (%t == LOCKED) {
    if (%phase == apply) || ($dccore.sw.s(saving.srv)) || ($dccore.sw.s(saving.oc)) { return $true }
    if ($dccore.sw.s(resending.oc)) || ($dccore.sw.s(w.onconnect) != $null) { return $true }
    return $false
  }
  if ($istok(UNLOCKED UNLOCK,%t,32)) { return $iif($dccore.sw.s(unlocking),$true,$false) }
  return $false
}
alias dccore.sw.m { return $hget(dccore.swm,$1) }
alias dccore.sw.s { return $hget(dccore.sws,$1) }
; Set an item of dccore.sws, or delete it when there is nothing to keep (an
; empty /hadd is an error).
alias dccore.sw.keep {
  if ($2- == $null) { hdel dccore.sws $1 }
  else { hadd dccore.sws $1 $2- }
}
; Put a text into a control: did -a refuses an empty one.
alias dccore.sw.put {
  did -r dccore.set $1
  if ($2- != $null) { did -a dccore.set $1 $2- }
}
; A static text shows a lone & as an underline under the next letter.
alias dccore.sw.status { if ($dialog(dccore.set)) { dccore.sw.put 1018 $replace($1-,&,&&) } }

; The generated texts: every character a script line could misread - $, %,
; |, braces, brackets, #, commas - is written ~HH there.
alias dccore.sw.untext { return $regsubex($1,/~([0-9A-F][0-9A-F])/g,$chr($base(\1,16,10))) }

; A value from the bot, and one going back (docs/ADMIN-CONSOLE.md, "The
; value encoding"). dec turns every %HH escape into its character in one
; pass. enc writes, in this order: a % that would read as an escape, the
; spaces that would not survive the trip (at the start, at the end, after
; another space), and control characters. Spaces are \x20 in the patterns:
; a space would end the argument. tok is a value that is not the last field
; of its line, so it stays one word.
alias dccore.sw.dec { return $regsubex($1,/%(0[0-9a-f]|1[0-9a-f]|2[05d]|7f)/gi,$chr($base(\1,16,10))) }
; One expression, the three steps nested: a /var in between would close up
; the very runs of spaces it is there to keep.
alias dccore.sw.enc { return $regsubex($regsubex($regsubex($1,/%(?=0[0-9a-f]|1[0-9a-f]|2[05d]|7f)/gi,$chr(37) $+ 25),/^\x20|\x20$|(?<=\x20)\x20/g,$chr(37) $+ 20),/([\x01-\x1f\x7f])/g,$chr(37) $+ $base($asc(\1),10,16,2)) }
; A preview line: encoded like any value (its colour codes as %03, its runs
; of spaces as %20), decoded here, then every space made a non-breaking one.
; echo collapses a run of spaces, and a theme's frame IS runs of spaces on a
; background colour; a coloured $chr(160) draws the same block. Decoded in
; place rather than through dccore.sw.dec, whose return would drop a space
; at the start or the end of the line.
alias dccore.sw.pvtext { return $replace($regsubex($1,/%(0[0-9a-f]|1[0-9a-f]|2[05d]|7f)/gi,$chr($base(\1,16,10))),$chr(32),$chr(160)) }
alias dccore.sw.tok {
  if ($1 == $null) { return - }
  if ($1 == -) { return $chr(37) $+ 2D }
  return $replace($dccore.sw.enc($1),$chr(32),$chr(37) $+ 20)
}
alias dccore.sw.untok {
  if ($1 == -) { return }
  return $dccore.sw.dec($1)
}
; An encoded value (enc's form, which may hold single spaces) as one token.
alias dccore.sw.retok { return $replace($1-,$chr(32),$chr(37) $+ 20) }

; The bot runs on this PC, so a path picked with the "..." buttons is one it
; can read: the console connection is loopback, or the bot's CAPS line ends
; with machine:<its computer name> and that is this computer's. The address
; alone cannot tell: a DCC chat to a bot on the same PC usually arrives from
; the public address the client advertises, never 127.0.0.1. A bot from
; before the machine token never counts as local over a real address.
alias dccore.sw.local {
  if ($istok(127.0.0.1 ::1,$chat($dccore.bot).ip,32)) { return $true }
  var %bot = $dccore.sw.machine
  if (%bot == $null) || (%bot == -) { return $false }
  ; == on purpose: a computer's name is the same name in any case
  return $iif(%bot == $dccore.sw.myhost,$true,$false)
}
; The bot's machine:<name> from its CAPS line, or nothing.
alias dccore.sw.machine { return $gettok($wildtok($dccore.sw.s(caps),machine:*,1,32),2-,58) }
; This computer's name made one token as the bot makes its own: a space, a
; colon or a control character is "-".
alias dccore.sw.myhost { return $regsubex($host,/[\x00-\x20:]/g,-) }

; Whether this bot has a part of the protocol (from its CAPS line), and the
; window is talking to it.
alias dccore.sw.can {
  if (!$dccore.sw.s(live)) { return $false }
  return $iif($wildtok($dccore.sw.s(caps),$1 $+ :*,1,32),$true,$false)
}

on *:dialog:dccore.set:init:0: { dccore.sw.init | dccore.theme.dialog dccore.set init | dccore.theme.dcxset }
on *:dialog:dccore.set:close:0: { dccore.sw.closed | if ($dialog(dccore.setl)) { dialog -x dccore.setl } }
on *:dialog:dccore.set:sclick:*: { dccore.sw.click $did }
on *:dialog:dccore.set:edit:*: { dccore.sw.touched $did }
on *:dialog:dccore.set:mouse:*: { dccore.sw.hover $did }

alias dccore.sw.init {
  dccore.sw.data
  if ($hget(dccore.sws)) { hfree dccore.sws }
  hmake dccore.sws 100
  ; the texts that could not be written into the dialog table as they are
  var %i = 1, %n = $hfind(dccore.swm,t.*,0,w)
  while (%i <= %n) {
    var %item = $hfind(dccore.swm,t.*,%i,w)
    dccore.sw.put $gettok(%item,2,46) $dccore.sw.untext($dccore.sw.m(%item))
    inc %i
  }
  dccore.sw.combos
  dccore.sw.opt.fill
  ; this mIRC's own switches, from dccore.ini
  var %all = $dccore.sw.m(locals)
  %i = 1
  while (%i <= $numtok(%all,32)) {
    if ($dccore.opt($gettok(%all,%i,32))) { did -c dccore.set $dccore.sw.m(lo. $+ $gettok(%all,%i,32)) }
    inc %i
  }
  %i = 1
  while (%i <= $dccore.sw.m(pages)) { dccore.sw.showpage %i -h | inc %i }
  dccore.sw.enable 0
  dccore.sw.dcx
  if ($dccore.sw.s(dcx)) { dccore.sw.side $iif($hget(dccore.live,sw.open) == mirc,2,0) }
  else { dccore.sw.tab $iif($hget(dccore.live,sw.open) == mirc,$dccore.sw.m(groups),1) }
  if ($hget(dccore.live,sw.open)) { hdel dccore.live sw.open }
  .timerdccoreSwTick 0 5 dccore.sw.tick
  dccore.sw.start
  ; what is made from here on is made after the init's dccore.theme.dialog
  hadd dccore.sws inited 1
}

; Every combo's lines: the choices with the dashboard's labels, the three
; answers of a setting that may be unset, the sixteen colours. A setting's
; menu only gets its recipe here, and its lines the first time its page is
; shown (dccore.sw.cb.fill). The bindings' Mode menus are filled now: the
; rows are drawn again as the lists change.
alias dccore.sw.combos {
  var %modes = $dccore.sw.untext($dccore.sw.m(modelabels))
  var %j = 1
  while (%j <= $dccore.sw.m(keys.n)) {
    var %keys = $dccore.sw.m(keys. $+ %j), %i = 1
    while (%i <= $numtok(%keys,32)) {
      var %k = $gettok(%keys,%i,32), %id = $gettok($dccore.sw.m(k. $+ %k),1,32), %kind = $gettok($dccore.sw.m(k. $+ %k),2,32)
      if (%kind == choice) { dccore.sw.cb.lazy %id choice %k }
      elseif (%kind == tri) { dccore.sw.cb.lazy %id tri }
      elseif (%kind == colour) {
        dccore.sw.cb.lazy %id colour themedefault
        dccore.sw.cb.lazy $calc(%id + 1) colour keepprevious
      }
      inc %i
    }
    inc %j
  }
  ; the Mode menus of the bindings rows, and of the row that adds one
  var %r = 1
  while (%r <= 15) { dccore.sw.fillfrom $calc($dccore.sw.srv.rowbase(%r) + 2) %modes | inc %r }
  dccore.sw.fillfrom 1772 %modes
}
; $1 a combo, $2- its lines separated by commas: in one didtok
alias dccore.sw.fillfrom { if ($2- != $null) { didtok dccore.set $1 44 $2- } }

; ---- menus filled when their page is first shown (#1382) -------------
;  A line of a combo costs mIRC some 0.6 ms, nearly 2 while the window is
;  made, whichever command adds it: the panel's menus were about 1 s of
;  every open. So a setting's menu and the mIRC side's colour menus get a
;  recipe in dccore.sws at open (cb.<id>), and their lines the first time
;  their page is shown (cbmade.<id>). Till then the line selected is kept
;  as a number (cbsel.<id>, 0 for none), and a colour menu's 18th line "As
;  set: ..." as its text (cbx.<id>). Every read and write of such a menu's
;  selection goes through dccore.sw.cb.sel and dccore.sw.cb.select, which
;  say the same thing either way.
;  The recipes: choice <KEY> (the setting's labels), tri (the layout's three
;  answers), colour <themedefault|keepprevious> (that line, then the
;  layout's sixteen colours), colours (the sixteen of dccore.colours), bg
;  ("none (mIRC's)", then those), span <values> (dccore.fillspan's own lines
;  for those values, then those).

; $1 a menu, $2- its recipe
alias dccore.sw.cb.lazy { hadd dccore.sws cb. $+ $1 $2- }
; Whether $1 is a menu still waiting for its lines
alias dccore.sw.cb.waits { return $iif(($dccore.sw.s(cb. $+ $1) != $null) && (!$dccore.sw.s(cbmade. $+ $1)),$true,$false) }
; $1 a menu: its selected line, 0 for none - as one number either way, so a
; selection kept and the same one shown compare equal (the dirty checks)
alias dccore.sw.cb.sel {
  var %s = $dccore.sw.s(cbsel. $+ $1)
  if (!$dccore.sw.cb.waits($1)) { %s = $did(dccore.set,$1).sel }
  return $iif(%s isnum 1-,%s,0)
}
; $1 a menu, $2 a line to select, 0 or nothing to select none. A line the
; menu has not got selects none, as did -c leaves a filled one (a colour
; written into dccore.ini by hand): a waiting menu is measured against the
; lines it will have (dccore.sw.cb.lines), so Apply and the dirty checks
; read the same either way
alias dccore.sw.cb.select {
  var %n = $iif($2 isnum 1-,$2,0)
  if ($dccore.sw.cb.waits($1)) {
    if (%n > $dccore.sw.cb.lines($1)) { %n = 0 }
    hadd dccore.sws cbsel. $+ $1 %n
    return
  }
  if (%n) { did -c dccore.set $1 %n }
  else { did -u dccore.set $1 }
}
; $1 a menu waiting for its lines: how many it will have, from its recipe
; (as dccore.sw.cb.fill adds them), and the "As set" line kept for it
alias dccore.sw.cb.lines {
  var %r = $dccore.sw.s(cb. $+ $1), %k = $gettok(%r,1,32), %n = 0
  if (%k == choice) { %n = $numtok($dccore.sw.m(ch. $+ $gettok(%r,2,32)),32) }
  elseif (%k == tri) { %n = $numtok($dccore.sw.untext($dccore.sw.m(tri)),44) }
  elseif (%k == colour) { %n = $calc(1 + $numtok($dccore.sw.untext($dccore.sw.m(colours)),44)) }
  elseif (%k == colours) { %n = $numtok($dccore.colours,44) }
  elseif (%k == bg) { %n = $calc(1 + $numtok($dccore.colours,44)) }
  elseif (%k == span) { %n = $calc($numtok($gettok(%r,2-,32),32) + $numtok($dccore.colours,44)) }
  if ($dccore.sw.s(cbx. $+ $1) != $null) { inc %n }
  return %n
}
; $1 a colour menu, $2- the text of its 18th line ("As set: ..."), or
; nothing for none: the line there is taken away, and this one added
alias dccore.sw.cb.extra {
  if ($dccore.sw.cb.waits($1)) { dccore.sw.keep cbx. $+ $1 $2- | return }
  if ($did(dccore.set,$1).lines >= 18) { did -d dccore.set $1 18 }
  if ($2- != $null) { did -a dccore.set $1 $2- }
}
; $1 a page: its menus' lines, the first time it is shown. Its controls are
; the layout's own list of them (p.<page>.<n>).
alias dccore.sw.cb.page {
  if ($dccore.sw.s(cbpage. $+ $1)) { return }
  hadd dccore.sws cbpage. $+ $1 1
  var %j = 1
  while (%j <= $dccore.sw.m(p. $+ $1 $+ .n)) {
    var %ids = $dccore.sw.m($+(p.,$1,.,%j)), %i = 1
    while (%i <= $numtok(%ids,44)) {
      if ($dccore.sw.cb.waits($gettok(%ids,%i,44))) { dccore.sw.cb.fill $gettok(%ids,%i,44) }
      inc %i
    }
    inc %j
  }
}
; $1 a menu: its lines from its recipe, each list in one didtok - where a
; line is a label of its own, joined on $chr(7), which no label has, so a
; label with a comma stays one line - then the "As set" line kept for it,
; and the line kept as selected. Once: marked before, so a failure is not
; a second set of lines.
alias dccore.sw.cb.fill {
  if (!$dccore.sw.cb.waits($1)) { return }
  hadd dccore.sws cbmade. $+ $1 1
  var %r = $dccore.sw.s(cb. $+ $1), %k = $gettok(%r,1,32)
  if (%k == choice) {
    var %key = $gettok(%r,2,32), %c = 1, %l
    while (%c <= $numtok($dccore.sw.m(ch. $+ %key),32)) { %l = $+(%l,$chr(7),$dccore.sw.untext($dccore.sw.m($+(cl.,%key,.,%c)))) | inc %c }
    if (%l != $null) { didtok dccore.set $1 7 $mid(%l,2) }
  }
  elseif (%k == tri) { dccore.sw.fillfrom $1 $dccore.sw.untext($dccore.sw.m(tri)) }
  elseif (%k == colour) { didtok dccore.set $1 7 $+($dccore.sw.untext($dccore.sw.m($gettok(%r,2,32))),$chr(7),$replace($dccore.sw.untext($dccore.sw.m(colours)),$chr(44),$chr(7))) }
  elseif (%k == colours) { didtok dccore.set $1 44 $dccore.colours }
  elseif (%k == bg) {
    did -a dccore.set $1 none (mIRC's)
    didtok dccore.set $1 44 $dccore.colours
  }
  elseif (%k == span) {
    var %own = $gettok(%r,2-,32), %i = 1, %l
    while (%i <= $numtok(%own,32)) { %l = $+(%l,$chr(44),$dccore.spanlabel($gettok(%own,%i,32))) | inc %i }
    if (%l != $null) { didtok dccore.set $1 44 $mid(%l,2) }
    didtok dccore.set $1 44 $dccore.colours
  }
  var %x = $dccore.sw.s(cbx. $+ $1), %sel = $dccore.sw.s(cbsel. $+ $1)
  if (%x != $null) { did -a dccore.set $1 %x }
  if (%sel isnum 1-) { did -c dccore.set $1 %sel }
}

; $1 a page, $2 -h or -v: every control of the page hidden or shown - and
; with DCX its tables' listviews with them, their native lists kept hidden,
; and the page's name put in the title (#1288). A page's DCX frames and
; tables are made the first time it is shown (#1382), so hiding one never
; shown touches no DCX id: there is none yet, and DCX halts on a missing one
alias dccore.sw.showpage {
  ; its menus' lines the first time, in either window, before it shows
  if ($2 == -v) { dccore.sw.cb.page $1 }
  var %j = 1
  while (%j <= $dccore.sw.m(p. $+ $1 $+ .n)) { did $2 dccore.set $dccore.sw.m($+(p.,$1,.,%j)) | inc %j }
  if (!$dccore.sw.s(dcx)) { return }
  ; the frames: the native boxes stay hidden, their DCX lines and titles
  ; come and go with the page
  var %bx = $dccore.sw.m(p. $+ $1 $+ .bx)
  if (%bx != $null) {
    did -h dccore.set %bx
    if ($2 == -v) { dccore.sw.dcxframes $1 }
    if ($dccore.sw.s(frmade. $+ $1)) { dccore.dcx xdid dccore.set $dccore.sw.m(p. $+ $1 $+ .fr) $iif($2 == -v,-s,-h) }
  }
  ; the tables: lv.id makes one the first time (dccore.sw.lv.need)
  var %lvs = $dccore.sw.m(p. $+ $1 $+ .lv), %i = 1
  while (%i <= $numtok(%lvs,32)) {
    var %lv = $gettok(%lvs,%i,32)
    did -h dccore.set %lv
    if ($2 == -v) || ($dccore.sw.s(lvmade. $+ %lv)) { dccore.dcx xdid dccore.set $dccore.sw.lv.id(%lv) $iif($2 == -v,-s,-h) }
    inc %i
  }
  if ($2 == -v) { dccore.dcx xdid dccore.set $gettok($dccore.sw.m(dcxtitle),1,32) -t $dccore.sw.untext($dccore.sw.m(p. $+ $1)) }
}
; $1 1 or 0: every control that edits the bot enabled or not. The "..."
; buttons only for a bot on this PC.
alias dccore.sw.enable {
  var %j = 1
  while (%j <= $dccore.sw.m(bot.n)) {
    if ($1) { did -e dccore.set $dccore.sw.m(bot. $+ %j) }
    else { did -b dccore.set $dccore.sw.m(bot. $+ %j) }
    inc %j
  }
  if ($1) && (!$dccore.sw.local) {
    var %i = 1, %n = $hfind(dccore.swm,br.*,0,w)
    while (%i <= %n) { did -b dccore.set $gettok($hfind(dccore.swm,br.*,%i,w),2,46) | inc %i }
  }
  if ($dccore.sw.s(dcx)) {
    ; a disabled listview is drawn light grey whatever its colours, so in
    ; the dark theme the tables stay enabled: their rows do nothing by
    ; themselves, the buttons that act on them are what is disabled. A
    ; table not made yet is left alone: dccore.sw.lv.make gives it lv.e
    var %lvs = $dccore.sw.m(lvs), %i = 1, %e = $iif(($1) || ($dccore.theme.dialogsdark),-e,-b)
    hadd dccore.sws lv.e %e
    while (%i <= $numtok(%lvs,32)) {
      if ($dccore.sw.s(lvmade. $+ $gettok(%lvs,%i,32))) { dccore.dcx xdid dccore.set $dccore.sw.lv.id($gettok(%lvs,%i,32)) %e }
      inc %i
    }
  }
}

; $1 a tab: its pages onto the buttons on the left (push-style radio buttons,
; 1020 up, one per slot; the slots it does not need hidden), and the page
; last shown in it.
alias dccore.sw.tab {
  hadd dccore.sws tab $1
  ; the bot's tab last shown, for the way back to its side from the start
  if ($1 < $dccore.sw.m(groups)) { hadd dccore.sws bottab $1 }
  var %pages = $dccore.sw.m(g. $+ $1 $+ .pages), %i = 1
  while (%i <= $dccore.sw.m(slots)) {
    if (%i <= $numtok(%pages,32)) {
      did -ra dccore.set $calc(1019 + %i) $replace($dccore.sw.untext($dccore.sw.m(p. $+ $gettok(%pages,%i,32))),&,&&)
      if (!$dccore.sw.s(dcx)) { did -v dccore.set $calc(1019 + %i) }
    }
    else { did -h dccore.set $calc(1019 + %i) }
    inc %i
  }
  if ($dccore.sw.s(dcx)) { dccore.sw.dcxpages %pages | dccore.sw.dcxtabsel }
  var %last = $dccore.sw.s(last. $+ $1)
  if (%last == $null) { %last = $gettok(%pages,1,32) }
  dccore.sw.page %last
}
; $1 a page (numbered across all the tabs): shown, the one before hidden,
; and its rows asked for the first time it is shown
alias dccore.sw.page {
  if ($dccore.sw.s(page)) { dccore.sw.showpage $dccore.sw.s(page) -h }
  hadd dccore.sws page $1
  hadd dccore.sws last. $+ $dccore.sw.s(tab) $1
  dccore.sw.showpage $1 -v
  ; showing a page shows every control of it: the bindings' spare rows go again
  if ($istok($dccore.sw.m(p. $+ $1 $+ .w),srvchans,32)) { dccore.sw.srv.rowsvis }
  ; its button pressed, every other one not
  var %at = $findtok($dccore.sw.m(g. $+ $dccore.sw.s(tab) $+ .pages),$1,1,32), %i = 1
  while (%i <= $dccore.sw.m(slots)) {
    if (%i == %at) { did -c dccore.set $calc(1019 + %i) }
    else { did -u dccore.set $calc(1019 + %i) }
    inc %i
  }
  if ($dccore.sw.s(dcx)) { dccore.sw.dcxshow $1 }
  did -r dccore.set 1012
  var %asks = $dccore.sw.m(p. $+ $1 $+ .ask), %i = 1
  while (%i <= $numtok(%asks,32)) {
    if (!$dccore.sw.s(asked. $+ $gettok(%asks,%i,32))) && (!$dccore.sw.keeps($gettok(%asks,%i,32))) { dccore.sw.ask $gettok(%asks,%i,32) }
    inc %i
  }
}
; Show the page a setting is on (the first one the bot refused).
alias dccore.sw.goto {
  var %page = $dccore.sw.m(pg. $+ $1), %g = 1
  if (%page == $null) { return }
  while (%g <= $dccore.sw.m(groups)) {
    if ($istok($dccore.sw.m(g. $+ %g $+ .pages),%page,32)) {
      if (!$dccore.sw.s(dcx)) { did -f dccore.set $calc(1000 + %g) }
      hadd dccore.sws last. $+ %g %page
      ; with DCX the page's side first, from the start page or the other side
      if ($dccore.sw.s(dcx)) && ($dccore.sw.s(side) != $iif(%g == $dccore.sw.m(groups),2,1)) { dccore.sw.side $iif(%g == $dccore.sw.m(groups),2,1) }
      dccore.sw.tab %g
      return
    }
    inc %g
  }
}

; ---- the DCX page list, title and tables (#1288) -----------------------
;  When DCX can be used the window is marked and gets the mockup's
;  navigation: the tab strip stays for the groups, the page buttons are
;  hidden under a DCX list of the chosen tab's pages (a row each, the whole
;  row highlighted), the page's name goes, in bold, into the row between
;  the tabs and the page, and each table - a native list control - is
;  hidden under a DCX listview with columns over its place. Every page,
;  control and line of logic stays the built-in window's: the list only
;  drives dccore.sw.page, as the buttons do, and a table is read and
;  written through dccore.sw.lv.*, which address whichever of the two is
;  there. /dccore dcx check showed what this rests on in a real mIRC - its
;  own controls keep raising their events in a marked dialog, and
;  $dialog().cw over the width in dialog units is the pixel scale. When DCX
;  cannot be used none of this runs and the window is exactly the built-in
;  one.
alias dccore.sw.dcx {
  if ($dccore.dcx.why != $null) { return }
  dccore.dcx Mark dccore.set dccore.sw.dcxcb
  hadd dccore.sws dcx 1
  ; the theme as the window is made, asked once (each asking may check the
  ; dll's hash again): dark, its dialogs drawn dark - what is made later
  ; takes its dark colours from this - and noct, dccore_ui.dll is what draws
  ; them so: DCX's texts then get their fonts without ClearType
  ; (dccore.sw.fontflags), mIRC's own dark mode keeps it
  hadd dccore.sws dark $iif($dccore.theme.dialogsdark,1,0)
  hadd dccore.sws noct $iif(($dccore.sw.s(dark)) && (!$dccore.theme.mircdark),1,0)
  ; every line of the window is the script's own, in one colour - the
  ; system's, so a theme is followed: Windows draws its tabs and frames in
  ; light greys, and the first tab with no left edge. The native tabs and
  ; page buttons go; the frames are hidden page by page (dccore.sw.showpage)
  hadd dccore.sws line $dccore.theme.sys(line)
  hadd dccore.sws face $dccore.theme.sys(face)
  hadd dccore.sws light $dccore.theme.sys(light)
  var %i = 1
  while (%i <= $dccore.sw.m(groups)) { did -h dccore.set $calc(1000 + %i) | inc %i }
  %i = 1
  while (%i <= $dccore.sw.m(slots)) { did -h dccore.set $calc(1019 + %i) | inc %i }
  ; the body frame under the tabs, then the tab strip - after the frame,
  ; since the chosen tab stands on its top line. A page's own frames are
  ; made the first time it is shown (dccore.sw.showpage)
  var %b = $dccore.sw.m(dcxbody)
  dccore.sw.dcxring $gettok(%b,1,32) $dccore.sw.dcxrect($gettok(%b,2-5,32))
  dccore.sw.dcxtabs
  dccore.sw.dcxhome
  ; the chosen tab's pages, where the page buttons were: filled by dccore.sw.tab
  var %t = $dccore.sw.m(dcxpages), %id = $gettok(%t,1,32), %rect = $dccore.sw.dcxrect($gettok(%t,2-5,32))
  dccore.dcx xdialog dccore.set -c %id listview %rect report noheader singlesel fullrow showsel
  dccore.dcx xdid dccore.set %id -t +l 0 $calc($gettok(%rect,3,32) - 4) Pages
  dccore.sw.dcxaa %id listview
  ; the page's name, bold, between the tabs and the page
  %t = $dccore.sw.m(dcxtitle)
  dccore.dcx xdialog dccore.set -c $gettok(%t,1,32) text $dccore.sw.dcxrect($gettok(%t,2-5,32)) nowrap noprefix noformat
  dccore.dcx xdid dccore.set $gettok(%t,1,32) -f $dccore.sw.fontflags(b) default 9 Tahoma
  ; the tables are made when first needed (dccore.sw.lv.need)
}
; DCX's texts and tables without ClearType. ClearType tints the edges of
; each letter red and blue, and light letters on the dark background look
; blurred by it. dccore_ui.dll gives mIRC's own controls a font without it;
; DCX draws with the font xdid -f gave it, so where the DLL darkens the
; window (noct, dccore.sw.dcx) each DCX text and table is given its font
; with the flag a (anti-aliased, no ClearType) as it is made. In mIRC's own
; dark mode mIRC's controls keep ClearType, and DCX's keep it with them.
; $1 the other flags, b for bold: "+b", or "+ab" there.
alias dccore.sw.fontflags { return $+(+,$iif($dccore.sw.s(noct),a),$1) }
; $1 a DCX text or listview just made with no font of its own, $2 its type,
; text or listview: where fonts go without ClearType (fontflags), the font
; DCX gave it, again with the flag a. That font is asked of the first one
; of each type a window makes, and kept: DCX gives every one of a type the
; same. One already anti-aliased, or a font with no name, stays as it is.
alias dccore.sw.dcxaa {
  if (!$dccore.sw.s(noct)) { return }
  var %f = $dccore.sw.s(deffont. $+ $2)
  if (%f == $null) {
    ; +flags charset size name
    var %q = $dccore.dcx(_xdid,dccore.set $1 font), %fl = $gettok(%q,1,32)
    %f = -
    if ($left(%fl,1) == +) && (a !isincs %fl) && ($gettok(%q,4,32) != $null) { %f = $+(+a,$remove($mid(%fl,2),d)) $gettok(%q,2-,32) }
    hadd dccore.sws deffont. $+ $2 %f
  }
  if (%f != -) { dccore.dcx xdid dccore.set $1 -f %f }
}
; $1 a DCX id, $2- a rectangle in pixels: a panel there in the line colour
alias dccore.sw.dcxline {
  dccore.dcx xdialog dccore.set -c $1 panel $2 $3 $4 $5
  dccore.dcx xdid dccore.set $1 -C +b $dccore.sw.s(line)
}
; $1 a DCX id, $2- a rectangle in pixels: a panel there in the line colour,
; shaped by its window region - so nothing else of it is drawn or clicked -
; as a ring a pixel wide with rounded corners: the inside first, then the
; outside less that (a region combined is the new one less the old). The
; corners are the midpoint circle's pixels, as GDI draws one with a pen, and
; the polygons run on pixel edges, so the ring is exactly those: a ring cut
; from GDI's rounded rectangle came out as dots, the frame of one uneven
alias dccore.sw.dcxring {
  dccore.sw.dcxline $1 $2 $3 $4 $5
  dccore.dcx xdid dccore.set $1 -R +p $dccore.sw.dcxpoly($4,$5,i,4)
  dccore.dcx xdid dccore.set $1 -R +ip $dccore.sw.dcxpoly($4,$5,o,4)
}
alias dccore.sw.dcxpt { return $+($1,$chr(44),$2) }
; $1 a width and $2 a height in pixels, $3 o for the outside or i for the
; inside, $4 the corners rounded - 4, or 2 for the top ones and a straight
; bottom: the polygon, the generator's top-left corner (a staircase from
; the left edge up to the top edge) mirrored to the other three
alias dccore.sw.dcxpoly {
  var %c = $dccore.sw.m(dcxcorner. $+ $3), %n = $numtok(%c,32), %i = 1, %tl, %tr, %br, %bl
  while (%i <= %n) {
    var %p = $gettok(%c,%i,32), %x = $gettok(%p,1,44), %y = $gettok(%p,2,44)
    %tl = %tl $dccore.sw.dcxpt(%x,%y)
    %tr = $dccore.sw.dcxpt($calc($1 - %x),%y) %tr
    %br = %br $dccore.sw.dcxpt($calc($1 - %x),$calc($2 - %y))
    %bl = $dccore.sw.dcxpt(%x,$calc($2 - %y)) %bl
    inc %i
  }
  if ($4 == 2) { var %e = $iif($3 == i,1,0) | return $dccore.sw.dcxpt(%e,$2) %tl %tr $dccore.sw.dcxpt($calc($1 - %e),$2) }
  return %tl %tr %br %bl
}
; $1 a page: each of its frames drawn - the ring, and over its top line the
; title on the window's face, in the dark theme light - hidden until the
; page is shown, when the native box under it is hidden instead
; (dccore.sw.showpage). Once a window, the first time the page is shown:
; a DCX id made twice is an error.
alias dccore.sw.dcxframes {
  if ($dccore.sw.s(frmade. $+ $1)) { return }
  hadd dccore.sws frmade. $+ $1 1
  var %bx = $dccore.sw.m(p. $+ $1 $+ .bx), %i = 1
  while (%i <= $numtok(%bx,44)) {
    var %f = $dccore.sw.m(fr. $+ $gettok(%bx,%i,44)), %base = $gettok(%f,1,32), %title = $calc(%base + 1)
    dccore.sw.dcxring %base $dccore.sw.dcxrect($gettok(%f,2-5,32))
    dccore.dcx xdialog dccore.set -c %title text $dccore.sw.dcxrect($gettok(%f,6-9,32)) nowrap center noprefix noformat
    dccore.sw.dcxaa %title text
    dccore.dcx xdid dccore.set %title -C +b $dccore.sw.s(face)
    if ($dccore.sw.s(dark)) { dccore.theme.dcxtext %title }
    dccore.dcx xdid dccore.set %title -t $gettok(%f,10-,32)
    dccore.dcx xdid dccore.set $+(%base,-,%title) -h
    inc %i
  }
}
; the tab strip: for each tab an outline panel in the line colour, a panel
; on the face inside it and a text with the tab's name - laid out by
; dccore.sw.dcxtabsel
alias dccore.sw.dcxtabs {
  var %t = $dccore.sw.m(dcxtabs), %i = 1
  while (%i <= $gettok(%t,6,32)) {
    var %id = $calc($gettok(%t,1,32) + $gettok(%t,2,32) * (%i - 1))
    dccore.sw.dcxline %id 0 0 1 1
    dccore.dcx xdialog dccore.set -c $calc(%id + 1) panel 0 0 1 1
    dccore.dcx xdialog dccore.set -c $calc(%id + 2) text 0 0 1 1 nowrap center noprefix noformat
    dccore.sw.dcxaa $calc(%id + 2) text
    dccore.dcx xdid dccore.set $calc(%id + 2) -t $dccore.sw.m(g. $+ %i)
    inc %i
  }
}
; the tabs laid out for the tab chosen: that one from the strip's top down
; onto the body frame's top line, on the window's face, so the two join; the
; others a little lower, ending above the line, on the lighter face. The
; outline's region is the tab's shape, round at the top and straight below,
; the face's the inside of that, so the outline shows as a line a pixel wide
; with no bottom. Both are hidden while they are moved and shaped, and shown
; again after: a region set does not redraw, so what a panel had drawn
; outside its new region - the chosen tab's extra height - stayed on the
; screen, and hiding it first has the window wipe all of it. The text a line
; of dialog text tall, in the middle - the dll has no vcenter. Not on the
; mIRC side, which has no strip: its tabs stay hidden
alias dccore.sw.dcxtabsel {
  if ($dccore.sw.s(side) == 2) { return }
  var %t = $dccore.sw.m(dcxtabs), %first = $gettok(%t,1,32), %stride = $gettok(%t,2,32), %top = $gettok(%t,3,32), %lift = $gettok(%t,5,32)
  var %line = $gettok($dccore.sw.dcxrect($gettok($dccore.sw.m(dcxbody),2-5,32)),2,32), %th = $gettok($dccore.sw.dcxrect(0 8 0 0),2,32), %i = 1
  while (%i <= $gettok(%t,6,32)) {
    var %id = $calc(%first + %stride * (%i - 1)), %tab = $dccore.sw.m(tab. $+ %i), %sel = $iif(%i == $dccore.sw.s(tab),1,0)
    var %r = $dccore.sw.dcxrect($gettok(%tab,1,32) $calc(%top + $iif(%sel,0,%lift)) $calc($gettok(%tab,1,32) + $gettok(%tab,2,32)) 1)
    var %x = $gettok(%r,1,32), %y = $gettok(%r,2,32), %w = $calc($gettok(%r,3,32) - %x), %h = $calc(%line - %y + %sel), %face = $iif(%sel,$dccore.sw.s(face),$dccore.sw.s(light))
    dccore.dcx xdid dccore.set $+(%id,-,$calc(%id + 2)) -h
    dccore.dcx xdid dccore.set %id -p %x %y %w %h
    dccore.dcx xdid dccore.set %id -R +p $dccore.sw.dcxpoly(%w,%h,o,2)
    dccore.dcx xdid dccore.set $calc(%id + 1) -p %x %y %w %h
    dccore.dcx xdid dccore.set $calc(%id + 1) -R +p $dccore.sw.dcxpoly(%w,%h,i,2)
    dccore.dcx xdid dccore.set $calc(%id + 1) -C +b %face
    dccore.dcx xdid dccore.set $calc(%id + 2) -p $calc(%x + 2) $calc(%y + 1 + $int($calc((%h - 1 - %th) / 2))) $calc(%w - 4) %th
    dccore.dcx xdid dccore.set $calc(%id + 2) -C +b %face
    dccore.dcx xdid dccore.set $+(%id,-,$calc(%id + 2)) -s
    inc %i
  }
}
; $1 the first of two DCX ids, $2- a rectangle in pixels: a pill - an
; outline in the line colour and, inside it, the lighter face - shaped
; like a frame, four round corners; its text is the caller's (dcxlabel)
alias dccore.sw.dcxpill {
  dccore.sw.dcxline $1 $2 $3 $4 $5
  dccore.dcx xdid dccore.set $1 -R +p $dccore.sw.dcxpoly($4,$5,o,4)
  dccore.dcx xdialog dccore.set -c $calc($1 + 1) panel $2 $3 $4 $5
  dccore.dcx xdid dccore.set $calc($1 + 1) -R +p $dccore.sw.dcxpoly($4,$5,i,4)
  dccore.dcx xdid dccore.set $calc($1 + 1) -C +b $dccore.sw.s(light)
}
; $1 a DCX id, $2-4 x, y and a width in pixels, $5 1 for bold, $6- the
; text: a line of text, a line of dialog text tall, centred, on the
; lighter face
alias dccore.sw.dcxlabel {
  dccore.dcx xdialog dccore.set -c $1 text $2 $3 $4 $dccore.sw.dcxth nowrap center noprefix noformat
  dccore.dcx xdid dccore.set $1 -C +b $dccore.sw.s(light)
  if ($5) { dccore.dcx xdid dccore.set $1 -f $dccore.sw.fontflags(b) default 8 Tahoma }
  else { dccore.sw.dcxaa $1 text }
  dccore.dcx xdid dccore.set $1 -t $6-
}
; a line of dialog text, in pixels
alias dccore.sw.dcxth { return $gettok($dccore.sw.dcxrect(0 8 0 0),2,32) }
; the start page - a pill for each side, Bot and mIRC, its name and a line
; on what is there - and the way back from a side: a pill with an arrow at
; the strip's left end and the side's name beside it. All of it made here;
; dccore.sw.side shows what the side shown has
alias dccore.sw.dcxhome {
  var %th = $dccore.sw.dcxth, %b = $dccore.sw.m(dcxback), %r = $dccore.sw.dcxrect($gettok(%b,2-5,32))
  dccore.sw.dcxpill $gettok(%b,1,32) %r
  dccore.sw.dcxlabel $calc($gettok(%b,1,32) + 2) $calc($gettok(%r,1,32) + 1) $calc($gettok(%r,2,32) + $int($calc(($gettok(%r,4,32) - %th) / 2))) $calc($gettok(%r,3,32) - 2) 1 $chr(8592)
  var %c = $dccore.sw.m(dcxcrumb)
  dccore.dcx xdialog dccore.set -c $gettok(%c,1,32) text $dccore.sw.dcxrect($gettok(%c,2-5,32)) nowrap noprefix noformat
  dccore.dcx xdid dccore.set $gettok(%c,1,32) -f $dccore.sw.fontflags(b) default 8 Tahoma
  var %i = 1
  while (%i <= $dccore.sw.m(home.n)) {
    var %p = $dccore.sw.m(home. $+ %i), %id = $gettok(%p,1,32)
    %r = $dccore.sw.dcxrect($gettok(%p,2-5,32))
    var %x = $calc($gettok(%r,1,32) + 2), %y = $calc($gettok(%r,2,32) + $int($calc(($gettok(%r,4,32) - 2 * %th) / 2))), %w = $calc($gettok(%r,3,32) - 4)
    dccore.sw.dcxpill %id %r
    dccore.sw.dcxlabel $calc(%id + 2) %x %y %w 1 $dccore.sw.m(home. $+ %i $+ .t)
    dccore.sw.dcxlabel $calc(%id + 3) %x $calc(%y + %th) %w 0 $dccore.sw.m(home. $+ %i $+ .s)
    dccore.dcx xdid dccore.set $calc(%id + 3) -C +t $dccore.theme.sys(grey)
    inc %i
  }
  dccore.sw.dcxstats
}
; the start page's figures: frames like a page's, with rows of a key and a
; value, or lines of fixed-width text (the running sends, as the side panel
; has them) - made here, filled by dccore.sw.home.fill
alias dccore.sw.dcxstats {
  var %i = 1, %grey = $dccore.theme.sys(grey)
  while (%i <= $dccore.sw.m(home.f.n)) {
    var %f = $dccore.sw.m(home.f. $+ %i), %title = $gettok(%f,2,32)
    dccore.sw.dcxring $gettok(%f,1,32) $dccore.sw.dcxrect($gettok(%f,3-6,32))
    dccore.dcx xdialog dccore.set -c %title text $dccore.sw.dcxrect($gettok(%f,7-10,32)) nowrap center noprefix noformat
    dccore.sw.dcxaa %title text
    dccore.dcx xdid dccore.set %title -C +b $dccore.sw.s(face)
    dccore.dcx xdid dccore.set %title -t $gettok(%f,12-,32)
    var %j = 1
    while (%j <= $dccore.sw.m($+(home.f.,%i,.n))) {
      var %r = $dccore.sw.m($+(home.f.,%i,.r.,%j)), %k = $gettok(%r,1,32), %v = $gettok(%r,2,32)
      dccore.dcx xdialog dccore.set -c %k text $dccore.sw.dcxrect($gettok(%r,3-6,32)) nowrap noprefix noformat
      if (%v) {
        dccore.sw.dcxaa %k text
        dccore.dcx xdid dccore.set %k -C +t %grey
        dccore.dcx xdid dccore.set %k -t $gettok(%r,11-,32)
        dccore.dcx xdialog dccore.set -c %v text $dccore.sw.dcxrect($gettok(%r,7-10,32)) nowrap noprefix noformat endellipsis
        dccore.sw.dcxaa %v text
      }
      else { dccore.dcx xdid dccore.set %k -f $dccore.sw.fontflags default 8 Lucida Console }
      inc %j
    }
    inc %i
  }
}
; the figures filled from what the bot sends for the side panel (dccore.live,
; st.*): when the start page shows, and on every status burst while it does
alias dccore.sw.home.fill {
  if (!$dialog(dccore.set)) || (!$dccore.sw.s(dcx)) || ($dccore.sw.s(side) != 0) { return }
  ; the library's totals come in every burst once asked for (#1359), from
  ; a bot that has them; asked once a window
  if (!$dccore.sw.s(asked.library)) && ($dccore.sw.can(library)) { hadd dccore.sws asked.library 1 | dccore.send library }
  var %i = 1
  while (%i <= $dccore.sw.m(home.f.n)) {
    var %f = $dccore.sw.m(home.f. $+ %i)
    dccore.sw.home.frame %i $gettok(%f,2,32) $gettok(%f,12-,32)
    inc %i
  }
}
; $1 a frame's number, $2 its title's DCX id, $3- its title: its rows filled.
; Not linked to the bot (dccore.linked, #1396), nothing the bot sent is shown
; as current - those are the last session's - only the nick and network,
; which are this mIRC's own; the Connection row says which, in colour
alias dccore.sw.home.frame {
  var %on = $dccore.linked
  if ($3 == Bot) {
    dccore.sw.home.row $1 1 $iif($dccore.bot,$dccore.bot,-)
    dccore.sw.home.row $1 2 $iif($network,$network,$iif($server,$server,-))
    var %since = $iif(%on,$dccore.st(st.started))
    dccore.sw.home.row $1 3 $iif(%since > 0,$asctime(%since,ddd d mmm HH:nn) ( $+ $dccore.dur($calc($ctime - %since)) $+ ),-)
    var %state = $dccore.st(state)
    if (%on) {
      dccore.sw.home.ink $1 4 on
      dccore.sw.home.row $1 4 $chr(9679) connected $iif($dccore.st(mode) == structured,(structured console),(plain console))
    }
    elseif ($istok(opening waiting banner auth password,%state,32)) {
      dccore.sw.home.ink $1 4 wait
      dccore.sw.home.row $1 4 $chr(9679) connecting: %state
    }
    else {
      dccore.sw.home.ink $1 4 off
      dccore.sw.home.row $1 4 $chr(9679) offline
    }
    return
  }
  if (!%on) && ($istok(Today Library,$3,32)) {
    var %j = 1
    while (%j <= $dccore.sw.m($+(home.f.,$1,.n))) { dccore.sw.home.row $1 %j - | inc %j }
    return
  }
  if ($3 == Sending) {
    var %used = $dccore.st(st.used), %slots = $iif(%on,$dccore.st(st.slots))
    dccore.dcx xdid dccore.set $2 -t Sending $iif(%slots != $null,%used $+ / $+ %slots)
    var %j = 1
    while (%j <= $dccore.sw.m($+(home.f.,$1,.n))) {
      var %l = $dccore.st(slot. $+ %j), %line
      if (!%on) { %line = $iif(%j == 1,(bot offline)) }
      elseif (%l != $null) {
        ; <nick> <sent> <total> <bps> <name>, as the side panel draws it
        var %pct = $iif($gettok(%l,3,32) > 0,$int($calc($gettok(%l,2,32) * 100 / $gettok(%l,3,32))),0)
        %line = > $dccore.fit($gettok(%l,1,32),9) $dccore.rfit($dccore.bytes($gettok(%l,3,32)),7) $dccore.rfit(%pct $+ $chr(37),4) $dccore.rfit($dccore.speed($gettok(%l,4,32)),9) $gettok(%l,5-,32)
      }
      elseif (%slots != $null) && (%j == $calc(%used + 1)) && (%used < %slots) { %line = ( $+ $calc(%slots - %used) free) }
      dccore.sw.home.row $1 %j %line
      inc %j
    }
    return
  }
  if ($3 == Today) {
    dccore.sw.home.row $1 1 $iif($dccore.st(st.sent) != $null,$dccore.st(st.sent) files / $dccore.bytes($dccore.st(st.bytes)),-)
    dccore.sw.home.row $1 2 $iif($dccore.st(st.qusers) == $null,-,$iif($dccore.st(st.qusers) == 0,(empty),$dccore.st(st.qusers) users with $dccore.st(st.qfiles) files))
    dccore.sw.home.row $1 3 $iif($dccore.st(st.record) != $null,$dccore.speed($dccore.st(st.record)),-)
    var %failed = $iif($dccore.st(st.failed) != $null,$dccore.st(st.failed),$dccore.st(failed))
    var %searches = $iif($dccore.st(st.searches) != $null,$dccore.st(st.searches),$dccore.st(searches))
    dccore.sw.home.row $1 4 $iif(%failed != $null,%failed,0)
    dccore.sw.home.row $1 5 $iif(%searches != $null,%searches,0)
    return
  }
  if ($3 == Library) {
    dccore.sw.home.row $1 1 $dccore.sw.home.lists
    dccore.sw.home.row $1 2 $dccore.sw.home.lib(files)
    dccore.sw.home.row $1 3 $dccore.sw.home.lib(size)
    dccore.sw.home.row $1 4 $dccore.sw.home.lib(rebuilt)
    dccore.sw.home.row $1 5 $dccore.sw.home.lib(index)
    dccore.sw.home.row $1 6 $iif($dccore.st(rebuild) != $null,$dccore.rebuild.short,idle)
    dccore.sw.home.row $1 7 $iif($dccore.st(packing) != $null,$gettok($dccore.st(packing),5-,32) for $gettok($dccore.st(packing),1,32),none)
  }
}
; $1 files, size, rebuilt or index: the library's totals from the LIBRARY
; and SEARCHINDEX lines of the last burst (#1359) - every list's files,
; folders and bytes added up, the newest rebuild and how long it took, the
; search index of the lists held from other bots - or - from a bot that
; has sent none (not asked yet, or an older bot)
alias dccore.sw.home.lib {
  if ($dccore.st(lib.1) == $null) { return - }
  var %i = 1, %files = 0, %folders = 0, %bytes = 0, %at = 0, %secs = 0
  while ($dccore.st(lib. $+ %i) != $null) {
    var %l = $dccore.st(lib. $+ %i)
    inc %files $gettok(%l,1,32)
    inc %folders $gettok(%l,2,32)
    inc %bytes $gettok(%l,3,32)
    if ($gettok(%l,4,32) > %at) { %at = $gettok(%l,4,32) | %secs = $gettok(%l,5,32) }
    inc %i
  }
  if ($1 == files) { return $dccore.num(%files) files $iif(%folders > 0,in $dccore.num(%folders) folders) }
  if ($1 == size) { return $dccore.bytes(%bytes) }
  if ($1 == rebuilt) { return $iif(%at > 0,$asctime(%at,ddd d mmm HH:nn) $+ $chr(44) took $dccore.dur(%secs),not since the bot was updated) }
  var %x = $dccore.st(libindex)
  if (%x == $null) { return - }
  if ($gettok(%x,3,32) == none) { return no list held }
  return $dccore.num($gettok(%x,1,32)) rows from $gettok(%x,2,32) lists $+ $chr(44) $iif($gettok(%x,3,32) == filling,still filling,up to date)
}
; $1 a frame's number, $2 a row's, $3- the text: into the row's value, or
; its line; nothing becomes a blank, which the dll takes where no text is
alias dccore.sw.home.row {
  var %r = $dccore.sw.m($+(home.f.,$1,.r.,$2)), %id = $iif($gettok(%r,2,32),$gettok(%r,2,32),$gettok(%r,1,32))
  dccore.dcx xdid dccore.set %id -t $iif($3 == $null,$dccore.nbsp,$3-)
}
; $1 a frame's number, $2 a row's, $3 on, wait or off: the row's text in
; green, amber or red (#1396) - set on every fill, so no colour outlives the
; state it was for. The theme has no such colours: these are darker on the
; light face and lighter on the dark one. Kept (home.ink) for
; dccore.theme.dcxtexts, which gives every value the theme's text colour
; once, just after the window is made - after its first fill
alias dccore.sw.home.ink {
  var %r = $dccore.sw.m($+(home.f.,$1,.r.,$2)), %id = $iif($gettok(%r,2,32),$gettok(%r,2,32),$gettok(%r,1,32))
  var %dark = $iif($dccore.theme.dialogsdark,1,0), %rgb
  if ($3 == on) { %rgb = $iif(%dark,$rgb(110,200,110),$rgb(0,128,0)) }
  elseif ($3 == wait) { %rgb = $iif(%dark,$rgb(230,170,50),$rgb(160,100,0)) }
  else { %rgb = $iif(%dark,$rgb(240,100,100),$rgb(192,0,0)) }
  hadd dccore.sws home.ink %id %rgb
  dccore.dcx xdid dccore.set %id -C +t %rgb
}
; the lists' names, (primary) after the primary one - asked for from the
; bot once, as the Lists page asks, the first time the start page shows
alias dccore.sw.home.lists {
  ; from the LIBRARY lines when a bot sends them (#1359): the name, the
  ; primary marked, and each list's files
  if ($dccore.st(lib.1) != $null) {
    var %i = 1, %out
    while ($dccore.st(lib. $+ %i) != $null) {
      var %l = $dccore.st(lib. $+ %i), %name = $gettok(%l,7-,32) $iif($gettok(%l,6,32) == 1,(primary)) $dccore.num($gettok(%l,1,32)) files
      %out = $iif(%out == $null,%name,%out $+ $chr(44) %name)
      inc %i
    }
    return %out
  }
  if (!$dccore.sw.s(srv.ok)) {
    if (!$dccore.sw.s(asked.served)) && ($dccore.sw.can(served)) { dccore.sw.ask served }
    return $iif($dccore.sw.s(live),loading...,-)
  }
  var %order = $dccore.sw.s(srv.order), %i = 1, %out
  while (%i <= $numtok(%order,32)) {
    var %id = $gettok(%order,%i,32), %name = $dccore.sw.srv.listname(%id) $iif($gettok($dccore.sw.s(srv.l. $+ %id),1,32) == 1,(primary))
    %out = $iif(%out == $null,%name,%out $+ $chr(44) %name)
    inc %i
  }
  return $iif(%out == $null,none,%out)
}
; $1 a side: 0 the start page, 1 the bot's settings, 2 this mIRC's. The
; start page's pills and the way back swap places; the bot's tabs show on
; its side only, and on the mIRC side its group - the last - fills the
; list straight away. A page shown is hidden on the way to the start,
; with the list, the page's name and the help under it
alias dccore.sw.side {
  hadd dccore.sws side $1
  var %h = $dccore.sw.m(home. $+ $dccore.sw.m(home.n)), %b = $dccore.sw.m(dcxback), %t = $dccore.sw.m(dcxtabs)
  var %pills = $+($gettok($dccore.sw.m(home.1),1,32),-,$calc($gettok(%h,1,32) + 3))
  var %way = $+($gettok(%b,1,32),-,$gettok($dccore.sw.m(dcxcrumb),1,32))
  var %tabs = $+($gettok(%t,1,32),-,$calc($gettok(%t,1,32) + $gettok(%t,2,32) * $gettok(%t,6,32) - 1))
  var %list = $gettok($dccore.sw.m(dcxpages),1,32), %title = $gettok($dccore.sw.m(dcxtitle),1,32), %figures = $dccore.sw.m(home.f.ids)
  if ($1 == 0) {
    if ($dccore.sw.s(page)) { dccore.sw.showpage $dccore.sw.s(page) -h | hdel dccore.sws page }
    dccore.dcx xdid dccore.set %way -h
    dccore.dcx xdid dccore.set %tabs -h
    dccore.dcx xdid dccore.set %list -h
    dccore.dcx xdid dccore.set %title -h
    did -h dccore.set 1012
    dccore.dcx xdid dccore.set %pills -s
    dccore.dcx xdid dccore.set %figures -s
    dccore.sw.home.fill
    return
  }
  dccore.dcx xdid dccore.set %pills -h
  dccore.dcx xdid dccore.set %figures -h
  dccore.dcx xdid dccore.set $gettok($dccore.sw.m(dcxcrumb),1,32) -t $iif($1 == 1,Bot,mIRC)
  dccore.dcx xdid dccore.set %way -s
  dccore.dcx xdid dccore.set %list -s
  dccore.dcx xdid dccore.set %title -s
  did -v dccore.set 1012
  if ($1 == 2) { dccore.dcx xdid dccore.set %tabs -h | dccore.sw.tab $dccore.sw.m(groups) | return }
  var %bt = $dccore.sw.s(bottab)
  dccore.sw.tab $iif(%bt,%bt,1)
}
; $1- the pages of the tab shown: the list filled with their names, a row
; each, without that being taken as a click
alias dccore.sw.dcxpages {
  var %id = $gettok($dccore.sw.m(dcxpages),1,32), %i = 1
  hadd dccore.sws dcxquiet 1
  dccore.dcx xdid dccore.set %id -r
  while (%i <= $numtok($1-,32)) {
    dccore.dcx xdid dccore.set %id -a 0 0 + $dccore.theme.pageicon($gettok($1-,%i,32)) 0 0 0 0 0 $dccore.sw.untext($dccore.sw.m(p. $+ $gettok($1-,%i,32)))
    inc %i
  }
  hdel dccore.sws dcxquiet
}
; $1- a rectangle in dialog units, "x y w h": the same in DCX's pixels. An
; identifier's parameters are split on commas, not spaces, so the four
; numbers arrive as one and are taken apart here - as $1 to $4 they were
; one number and three blanks, and every DCX control was made zero-sized.
alias dccore.sw.dcxrect {
  var %r = $1-, %sx = $calc($dialog(dccore.set).cw / $gettok($dccore.sw.m(size),1,32)), %sy = $calc($dialog(dccore.set).ch / $gettok($dccore.sw.m(size),2,32))
  return $round($calc($gettok(%r,1,32) * %sx),0) $round($calc($gettok(%r,2,32) * %sy),0) $round($calc($gettok(%r,3,32) * %sx),0) $round($calc($gettok(%r,4,32) * %sy),0)
}
; $1 a native list: hidden, and the DCX listview with its columns made over
; its place - hidden too, until its page is shown. Made when first needed
; (dccore.sw.lv.need), so it takes here what the window gave the others:
; its font, the state dccore.sw.enable last gave the tables (lv.e; made
; enabled) and in the dark theme the lists' colours. Made after the window
; was, where dccore_ui.dll darkens it, the dll once more: it gives a
; listview its dark scroll bars and header (dccore.sw.lv.dark).
alias dccore.sw.lv.make {
  var %l = $dccore.sw.m(lv. $+ $1), %id = $gettok(%l,1,32), %rect = $dccore.sw.dcxrect($gettok(%l,2-5,32))
  did -h dccore.set $1
  dccore.dcx xdialog dccore.set -c %id listview %rect report singlesel fullrow showsel $dccore.sw.m(lvx. $+ $1)
  ; the columns: widths in dialog units made pixels, the last one the rest
  var %cols = $dccore.sw.m(lvc. $+ $1), %i = 1, %left = $calc($gettok(%rect,3,32) - 20), %sx = $calc($gettok(%rect,3,32) / $gettok(%l,4,32)), %out
  while (%i <= $numtok(%cols,44)) {
    var %c = $gettok(%cols,%i,44), %w = $round($calc($gettok(%c,1,32) * %sx),0)
    if (%w == 0) { %w = %left }
    %left = $calc(%left - %w)
    %out = $+(%out,$iif(%i > 1,$chr(9)),+l 0 %w $gettok(%c,2-,32))
    inc %i
  }
  dccore.dcx xdid dccore.set %id -t %out
  dccore.sw.dcxaa %id listview
  if ($dccore.sw.s(lv.e) == -b) { dccore.dcx xdid dccore.set %id -b }
  if ($dccore.sw.s(dark)) { dccore.theme.dcxlist %id }
  dccore.dcx xdid dccore.set %id -h
  ; in a timer, so a failure there halts nothing of this; 250 ms, and the
  ; same name restarts it, so tables made close together - the settings
  ; snapshot's and the served lists' - usually share one pass of the dll
  if ($dccore.sw.s(inited)) && ($dccore.sw.s(noct)) { .timerdccoreThemeLv -m 1 250 dccore.sw.lv.dark }
}
; The dll once more over the control panel, for the tables made since it
; opened - only while the theme it opened with still holds. The panel takes
; a theme change only when it opens again: dccore.theme.dialog reads the
; theme now, and after /dccore theme light it would turn mIRC's controls
; light under DCX parts that stay dark. (An if evaluates all of its
; conditions; each of these is safe to ask with the window gone.)
alias dccore.sw.lv.dark {
  if ($dialog(dccore.set)) && ($dccore.sw.s(noct)) && ($dccore.theme.dialogsdark) && (!$dccore.theme.mircdark) { dccore.theme.dialog dccore.set }
}
; $1 the page now shown: its row selected in the list, without that
; selection being taken as a click
alias dccore.sw.dcxshow {
  var %g = $dccore.sw.s(tab), %at = $findtok($dccore.sw.m(g. $+ %g $+ .pages),$1,1,32)
  if (!%at) { return }
  hadd dccore.sws dcxquiet 1
  dccore.dcx xdid dccore.set $gettok($dccore.sw.m(dcxpages),1,32) -c %at
  hdel dccore.sws dcxquiet
}
; DCX's events: $1 the dialog, $2 the event, $3 the control, $4- the row
alias dccore.sw.dcxcb {
  if ($dccore.sw.s(dcxquiet)) { return }
  ; the way back, or a side's pill on the start page: any part of it
  if ($2 == sclick) && ($3 isnum) {
    var %b = $gettok($dccore.sw.m(dcxback),1,32), %h = $gettok($dccore.sw.m(home.1),1,32)
    if ($3 >= %b) && ($3 < $calc(%b + 3)) { dccore.sw.go side 0 | return }
    if ($3 >= %h) && ($3 < $calc(%h + 4 * $dccore.sw.m(home.n))) { dccore.sw.go side $calc($int($calc(($3 - %h) / 4)) + 1) | return }
  }
  ; a tab of the strip clicked, its outline or its text
  if ($2 == sclick) && ($3 isnum) && ($3 >= $gettok($dccore.sw.m(dcxtabs),1,32)) {
    var %g = $calc($int($calc(($3 - $gettok($dccore.sw.m(dcxtabs),1,32)) / $gettok($dccore.sw.m(dcxtabs),2,32))) + 1)
    if (%g <= $dccore.sw.m(groups)) { dccore.sw.go tab %g | return }
  }
  ; a page picked in the list: the Nth page of the tab shown
  if ($3 != $gettok($dccore.sw.m(dcxpages),1,32)) || (!$istok(sclick selected,$2,32)) || ($4 !isnum) { return }
  var %p = $gettok($dccore.sw.m(g. $+ $dccore.sw.s(tab) $+ .pages),$4,32)
  if (%p == $null) || (%p == $dccore.sw.s(page)) { return }
  dccore.sw.go page %p
}

; ---- the tables ------------------------------------------------------
;  Each table is a native list control, with DCX hidden under a listview
;  with columns (dccore.sw.lv.make). Everything that reads or writes one
;  goes through these, which say the same thing to either, and every DCX
;  command on a table takes its id from dccore.sw.lv.id.

; $1 a native list: the control to address - the listview over it, made
; the first time it is needed, or itself
alias dccore.sw.lv.id {
  if (!$dccore.sw.s(dcx)) { return $1 }
  dccore.sw.lv.need $1
  return $gettok($dccore.sw.m(lv. $+ $1),1,32)
}
; $1 a native list: with DCX, its listview made once a window, the first
; time its page shows it or anything reads or writes it (#1382) - not at
; open, where each one costs time and some GDI objects that DCX never gives
; back (#1381). A page shown, the bot's rows or an Apply may come first.
alias dccore.sw.lv.need {
  if (!$dccore.sw.s(dcx)) || ($dccore.sw.s(lvmade. $+ $1)) { return }
  hadd dccore.sws lvmade. $+ $1 1
  dccore.sw.lv.make $1
}
alias dccore.sw.lv.clear {
  if ($dccore.sw.s(dcx)) { dccore.dcx xdid dccore.set $dccore.sw.lv.id($1) -r }
  else { did -r dccore.set $1 }
}
; $1 a native list, $2- a row's cells as tokens (encoded, "-" for an empty
; one - dccore.sw.tok's form): one column each in a listview, joined with
; " - " in a native list
alias dccore.sw.lv.add {
  var %i = 1, %row
  while (%i <= $numtok($2-,32)) {
    var %cell = $dccore.sw.untok($gettok($2-,%i,32))
    if ($dccore.sw.s(dcx)) { %row = $+(%row,$iif(%i > 1,$+($chr(9),+ 0 0 0 0 $chr(32))),$iif(%cell != $null,%cell,$chr(160))) }
    elseif (%cell != $null) { %row = $+(%row,$iif(%row != $null,$+($chr(32),-,$chr(32))),%cell) }
    inc %i
  }
  if ($dccore.sw.s(dcx)) { dccore.dcx xdid dccore.set $dccore.sw.lv.id($1) -a 0 0 + 0 0 0 0 0 0 %row }
  else { did -a dccore.set $1 %row }
}
; $1 a native list: its selected row's number, or nothing
alias dccore.sw.lv.sel {
  if ($dccore.sw.s(dcx)) { var %s = $dccore.dcx(_xdid,dccore.set $dccore.sw.lv.id($1) sel) | return $iif(%s isnum 1-,%s) }
  return $did(dccore.set,$1).sel
}
alias dccore.sw.lv.count {
  if ($dccore.sw.s(dcx)) { return $dccore.dcx(_xdid,dccore.set $dccore.sw.lv.id($1) num) }
  return $did(dccore.set,$1).lines
}
; $1 a native list, $2 a row: its first cell
alias dccore.sw.lv.text {
  if ($dccore.sw.s(dcx)) { return $dccore.dcx(_xdid,dccore.set $dccore.sw.lv.id($1) text $2) }
  return $did(dccore.set,$1,$2).text
}
alias dccore.sw.lv.del {
  if ($dccore.sw.s(dcx)) { dccore.dcx xdid dccore.set $dccore.sw.lv.id($1) -d $2 }
  else { did -d dccore.set $1 $2 }
}

; ---- talking to the bot ----------------------------------------------

; consolecaps first: a bot from before the window answers "Unknown
; command" (dccore.sw.old), and the window then edits nothing of it.
alias dccore.sw.start {
  .timerdccoreSwCaps off
  hdel dccore.sws live
  hdel dccore.sws loaded
  hdel -w dccore.sws asked.*
  ; nothing asked before this start is still awaited
  hdel -w dccore.sws w.*
  dccore.sw.enable 0
  dccore.unstale
  if ($dccore.st(mode) != structured) || (!$chat($dccore.bot)) {
    dccore.sw.put 1013 Not connected to the bot
    dccore.sw.status Not connected to the bot: /dccore connect, then Reload. Only this mIRC's own switches can be changed now.
    hadd dccore.sws phase off
    return
  }
  dccore.sw.put 1013 Connected to $dccore.bot - changes apply when you press Apply or OK
  hadd dccore.sws phase caps
  dccore.sw.status Asking $dccore.bot what its console can do...
  dccore.send consolecaps
  .timerdccoreSwCaps 1 20 dccore.sw.nocaps
}
alias dccore.sw.nocaps {
  if (!$dialog(dccore.set)) || ($dccore.sw.s(phase) != caps) { return }
  hadd dccore.sws phase off
  dccore.sw.status $dccore.bot did not answer. Press Reload to ask again.
}
; A bot older than the window: "Unknown command: consolecaps". $true when
; that is what this OUT line is, so it is not also shown in @DCCore.
alias dccore.sw.old {
  if (!$dialog(dccore.set)) { return $false }
  if ($dccore.sw.s(phase) != caps) { return $false }
  if (Unknown command: consolecaps* !iswm $1-) { return $false }
  .timerdccoreSwCaps off
  hadd dccore.sws phase off
  dccore.sw.put 1013 $dccore.bot is too old for the settings window
  dccore.sw.status This bot is too old for the settings window - update it. Only this mIRC's own switches can be changed here.
  return $true
}
; The console closed, or a new one opened (HELLO): start again - but
; edits not saved yet are kept (keep): the reload after it refreshes only
; the controls nobody touched, and the structured pages with edits of their
; own are not asked for again. Reload discards them.
alias dccore.sw.lost {
  if (!$dialog(dccore.set)) { return }
  if ($dccore.sw.s(loaded)) {
    if ($dccore.sw.changed != $null) || ($dccore.sw.unsaved != $null) { hadd dccore.sws keep 1 }
  }
  dccore.sw.start
  ; the start page says the link dropped or came up now, not at Reload (#1396)
  dccore.sw.home.fill
}
alias dccore.sw.load {
  hadd dccore.sws phase load
  dccore.sw.wait settings
  hdel dccore.sws loaded
  dccore.sw.enable 0
  if ($dccore.sw.s(after) == $null) { dccore.sw.status Loading the settings from $dccore.bot $+ ... }
  dccore.send settings
}
; One of the structured snapshots: onconnect, served, folders, banlist.
alias dccore.sw.ask {
  if (!$dccore.sw.can($1)) { return }
  hadd dccore.sws asked. $+ $1 1
  dccore.sw.wait $1
  dccore.send $1
}
alias dccore.sw.reask { if ($dialog(dccore.set)) { hdel dccore.sws asked. $+ $1 | dccore.sw.ask $1 } }
; A snapshot or an answer that never comes: the bot gives up on a client
; that stalls for 15 seconds and ends the snapshot with an OUT line and no
; END, and a link can die mid-save. Every ask notes when to give up
; (w.<what>: settings for every SET* exchange, else the command asked),
; every END or SETDONE clears it, and a ticker looks every five seconds -
; so a missing END is a failed load, not a window waiting for ever.
alias dccore.sw.wait { hadd dccore.sws w. $+ $1 $calc($ctime + 30) }
alias dccore.sw.waited { hdel dccore.sws w. $+ $1 }
alias dccore.sw.tick {
  if (!$dialog(dccore.set)) { .timerdccoreSwTick off | return }
  var %i = $hfind(dccore.sws,w.*,0,w)
  while (%i >= 1) {
    var %item = $hfind(dccore.sws,w.*,%i,w)
    if ($ctime > $dccore.sw.s(%item)) { hdel dccore.sws %item | dccore.sw.timeout $gettok(%item,2,46) }
    dec %i
  }
}
alias dccore.sw.timeout {
  if ($1 == settings) {
    if ($istok(load apply preview aborting,$dccore.sw.s(phase),32)) { hadd dccore.sws phase idle }
    dccore.sw.status No complete answer from the bot - nothing changed here. Press Reload.
    return
  }
  if ($1 == applied) {
    hdel dccore.sws applying
    dccore.sw.status Saved - the bot has not said it has applied them yet. Reload shows what it has now.
    return
  }
  if ($1 == unlock) {
    hdel dccore.sws unlocking
    hdel dccore.sws relock
    dccore.sw.status No answer to the password - nothing was saved. Your changes are still here.
    return
  }
  hdel dccore.sws asked. $+ $1
  dccore.sw.status No complete answer from the bot to $1 - press Refresh or open the page again.
}
alias dccore.sw.reload {
  if ($istok(apply confirm aborting preview locked,$dccore.sw.s(phase),32)) { dccore.sw.status Still waiting for the bot... | return }
  hdel dccore.sws after
  ; Reload is how kept edits are discarded
  hdel dccore.sws keep
  hdel dccore.sws srv.dirty
  dccore.sw.start
}

; Every line of the settings protocol, while the window is open.
alias dccore.sw.line {
  if (!$dialog(dccore.set)) { return }
  var %t = $1
  if (%t == CAPS) {
    .timerdccoreSwCaps off
    hadd dccore.sws caps $2-
    if (!$wildtok($2-,settings:*,1,32)) {
      hadd dccore.sws phase off
      dccore.sw.status This bot's console has no settings - update the bot.
      return
    }
    hadd dccore.sws live 1
    dccore.sw.load
    ; the start page asks for the lists and the library's totals now
    ; that it can, not at the next burst (#1359); a page shown again
    if ($dccore.sw.s(side) == 0) { dccore.sw.home.fill }
    else { dccore.sw.page $dccore.sw.s(page) }
    return
  }
  ; settings: SETBEGIN <n>, SETF <KEY> <type> [value], SETEND <n>
  if (%t == SETBEGIN) {
    hdel -w dccore.sws v.*
    hdel -w dccore.sws na.*
    hadd dccore.sws setwant $2
    hadd dccore.sws setgot 0
    hadd dccore.sws unknown 0
    return
  }
  if (%t == SETF) {
    hinc dccore.sws setgot
    if ($dccore.sw.m(k. $+ $2) == $null) { hinc dccore.sws unknown | return }
    ; kept ENCODED, as sent: an encoded value has no run of spaces and no
    ; space at either end, so it survives /hadd and a command's parameters,
    ; which a decoded one would not
    hadd dccore.sws v. $+ $2 = $+ $4-
    return
  }
  if (%t == SETEND) {
    dccore.sw.waited settings
    if ($dccore.sw.s(setgot) != $2) || ($dccore.sw.s(setwant) != $2) {
      hadd dccore.sws phase idle
      dccore.sw.status The bot's answer was cut short: $dccore.sw.s(setgot) of $2 settings arrived, and nothing can be edited from half of them. Press Reload.
      return
    }
    dccore.sw.enable 1
    dccore.sw.fillall $dccore.sw.s(keep)
    hadd dccore.sws loaded 1
    hadd dccore.sws phase idle
    if ($dccore.sw.s(keep)) {
      hdel dccore.sws keep
      hadd dccore.sws after Reconnected - your unsaved changes are kept; press Reload to discard them.
    }
    var %said = $iif($dccore.sw.s(after),$dccore.sw.s(after),Loaded $2 settings from $dccore.bot $+ .)
    hdel dccore.sws after
    if ($dccore.sw.s(unknown)) { %said = %said The bot has $dccore.sw.s(unknown) more this window has no place for - update dccore.mrc. }
    dccore.sw.status %said
    return
  }
  ; a transaction: SETOPEN, SETERR <KEY> <message>, SETDONE <status> ...
  if (%t == SETOPEN) { return }
  if (%t == SETERR) {
    if ($dccore.sw.s(errkey) == $null) { hadd dccore.sws errkey $2 }
    dccore.sw.keep errs $dccore.sw.s(errs) $dccore.sw.label($2) $+ : $3-
    return
  }
  if (%t == SETDONE) { dccore.sw.done $2- | return }
  ; the save's rehash has finished: now the bot's values are the new ones
  if (%t == SETAPPLIED) {
    dccore.sw.waited applied
    var %said = Saved and applied $dccore.sw.s(applying) setting(s).
    hdel dccore.sws applying
    if ($dccore.sw.s(phase) != idle) || ($dccore.sw.changed != $null) { dccore.sw.status %said Your newer changes are not saved yet - Reload discards them. | return }
    hadd dccore.sws after %said
    dccore.sw.load
    return
  }
  ; the lock: LOCKED <command> <sentence>, UNLOCKED, UNLOCK error <message>
  if (%t == LOCKED) { dccore.sw.locked $2- | return }
  if (%t == UNLOCKED) { dccore.sw.unlocked | return }
  if (%t == UNLOCK) { dccore.sw.unlockfailed $3- | return }
  ; the theme preview: PVBEGIN 2, PVLINE <advert|notice> <raw line>, PVEND 2
  if (%t == PVBEGIN) {
    hadd dccore.sws pvgot 0
    if (!$window(@DCCore-preview)) { window @DCCore-preview }
    else { clear @DCCore-preview }
    echo 14 @DCCore-preview The theme as set in the settings window, not saved yet:
    return
  }
  if (%t == PVLINE) {
    hinc dccore.sws pvgot
    echo @DCCore-preview $+($chr(3),14,$2,:,$chr(15)) $dccore.sw.pvtext($3-)
    return
  }
  if (%t == PVEND) {
    if ($dccore.sw.s(pvgot) != $2) { dccore.sw.status The preview was cut short - press Preview again. | return }
    window -a @DCCore-preview
    dccore.sw.status The sample advert and notice are in @DCCore-preview.
    return
  }
  ; the served lists: SRVBEGIN <lists> <source> <max>, SRVLIST <n> <0|1>
  ; <name>, SRVCHAN <n> <channel> <mode>, SRVFOLDER <n> <label> <path>,
  ; SRVEND <lists> <channels> <folders>
  if (%t == SRVBEGIN) {
    hdel -w dccore.sws srv.*
    hadd dccore.sws srv.src $3
    hadd dccore.sws srv.max $4
    hadd dccore.sws srv.got 0 0 0
    hadd dccore.sws srv.next 0
    hadd dccore.sws srv.cnext 0
    hadd dccore.sws srv.fnext 0
    return
  }
  if (%t == SRVLIST) {
    dccore.sw.srv.count 1
    hinc dccore.sws srv.next
    var %id = $dccore.sw.s(srv.next)
    hadd dccore.sws srv.l. $+ %id $3 $4-
    hadd dccore.sws srv.order $dccore.sw.s(srv.order) %id
    hadd dccore.sws srv.n. $+ $2 %id
    return
  }
  if (%t == SRVCHAN) {
    dccore.sw.srv.count 2
    dccore.sw.srv.addchan $dccore.sw.s(srv.n. $+ $2) $3 $4
    return
  }
  if (%t == SRVFOLDER) {
    dccore.sw.srv.count 3
    dccore.sw.srv.addfolder $dccore.sw.s(srv.n. $+ $2) $3 $4-
    return
  }
  if (%t == SRVEND) {
    dccore.sw.waited served
    if ($dccore.sw.s(srv.got) != $2-4) {
      hdel dccore.sws srv.ok
      dccore.sw.status The lists arrived cut short ( $+ $dccore.sw.s(srv.got) of $2-4 $+ ) - press Refresh on Lists.
      return
    }
    hadd dccore.sws srv.ok 1
    hdel dccore.sws srv.dirty
    hadd dccore.sws srv.sig0 $dccore.sw.srv.sig
    dccore.sw.srv.draw
    dccore.sw.home.fill
    ; no lists.json: the one implied list's folders are the served folders,
    ; which the folders command edits without making the list real
    if ($dccore.sw.s(srv.src) == implied) { dccore.sw.reask folders }
    return
  }
  if (%t == SRVOPEN) || (%t == FLDOPEN) || (%t == OCOPEN) { return }
  if (%t == SRVERR) || (%t == FLDERR) { dccore.sw.keep srv.errs $dccore.sw.s(srv.errs) $2- | return }
  if (%t == SRVDONE) || (%t == FLDDONE) {
    hdel dccore.sws saving.srv
    var %errs = $dccore.sw.s(srv.errs)
    hdel dccore.sws srv.errs
    ; only a save that went through clears the edits: a refused one keeps
    ; them, so OK still says they are not saved
    if ($2 == ok) { hdel dccore.sws srv.dirty | dccore.sw.status $4- | dccore.sw.reask served | dccore.sw.maybeclose | return }
    if ($2 == unchanged) { hdel dccore.sws srv.dirty | dccore.sw.status $3- | dccore.sw.maybeclose | return }
    if ($2 == error) { hdel dccore.sws close | dccore.sw.status Not saved: %errs $3- Your changes are still here: correct them and Apply again. | return }
    return
  }
  ; the folders of a bot with no lists.json: FLDBEGIN <n> <source>,
  ; FLDROW <n> <label> <path>, FLDEND <n>
  if (%t == FLDBEGIN) {
    hdel -w dccore.sws fld.*
    hadd dccore.sws fld.want $2
    hadd dccore.sws fld.src $3
    hadd dccore.sws fld.got 0
    return
  }
  if (%t == FLDROW) {
    hinc dccore.sws fld.got
    hadd dccore.sws fld.r. $+ $dccore.sw.s(fld.got) $3 $4-
    return
  }
  if (%t == FLDEND) {
    dccore.sw.waited folders
    if ($dccore.sw.s(fld.got) != $2) || ($dccore.sw.s(fld.want) != $2) { dccore.sw.status The served folders arrived cut short - press Refresh on Lists. | return }
    dccore.sw.srv.implied
    return
  }
  ; the on-connect commands: OCBEGIN <n> <delay> <max> <max delay>,
  ; OCLINE <n> <command>, OCEND <n>
  if (%t == OCBEGIN) {
    hdel -w dccore.sws oc.*
    hadd dccore.sws oc.want $2
    hadd dccore.sws oc.delay $3
    hadd dccore.sws oc.got 0
    return
  }
  if (%t == OCLINE) {
    hinc dccore.sws oc.got
    hadd dccore.sws oc.l. $+ $dccore.sw.s(oc.got) = $+ $3-
    return
  }
  if (%t == OCEND) {
    dccore.sw.waited onconnect
    if ($dccore.sw.s(oc.got) != $2) || ($dccore.sw.s(oc.want) != $2) { dccore.sw.status The on-connect commands arrived cut short - open IRC Server again. | hdel dccore.sws asked.onconnect | return }
    dccore.sw.oc.fill $2
    return
  }
  if (%t == OCERR) { dccore.sw.keep oc.errs $dccore.sw.s(oc.errs) $2- | return }
  if (%t == OCDONE) {
    hdel dccore.sws saving.oc
    var %errs = $dccore.sw.s(oc.errs)
    hdel dccore.sws oc.errs
    if ($2 == ok) { dccore.sw.status $4- | dccore.sw.reask onconnect | dccore.sw.maybeclose | return }
    if ($2 == unchanged) { dccore.sw.status $3- | dccore.sw.maybeclose | return }
    if ($2 == error) { hdel dccore.sws close | dccore.sw.status Not saved: %errs $3- | return }
    return
  }
  if (%t == OCRESEND) {
    hdel dccore.sws resending.oc
    if ($2 == ok) { dccore.sw.status $4- | return }
    dccore.sw.status Not sent: $3-
    return
  }
  ; the bans: BANBEGIN <permanent> <timed>, BANP <pattern>, BANT <seconds
  ; left> <nick>, BANEND <permanent> <timed> - at most 200 rows of each
  if (%t == BANBEGIN) {
    hdel -w dccore.sws ban.*
    hadd dccore.sws ban.gp 0
    hadd dccore.sws ban.gt 0
    return
  }
  if (%t == BANP) {
    hinc dccore.sws ban.gp
    hadd dccore.sws ban.p. $+ $dccore.sw.s(ban.gp) $dccore.sw.dec($2-)
    return
  }
  if (%t == BANT) {
    hinc dccore.sws ban.gt
    hadd dccore.sws ban.t. $+ $dccore.sw.s(ban.gt) $2 $dccore.sw.untok($3)
    return
  }
  if (%t == BANEND) {
    dccore.sw.waited banlist
    if ($dccore.sw.s(ban.gp) != $iif($2 > 200,200,$2)) || ($dccore.sw.s(ban.gt) != $iif($3 > 200,200,$3)) {
      dccore.sw.status The bans arrived cut short - press Refresh on Bans & ignores.
      return
    }
    dccore.sw.ban.draw $2 $3
    return
  }
}

; SETDONE <status> ...: the end of Apply, of the preview's transaction, or
; of a question answered.
alias dccore.sw.done {
  dccore.sw.waited settings
  var %phase = $dccore.sw.s(phase)
  var %errkey = $dccore.sw.s(errkey)
  var %errs = $dccore.sw.s(errs)
  hdel dccore.sws errs
  hdel dccore.sws errkey
  if ($1 == confirm) {
    ; clearing DEBUG_CHANNEL: the bot leaves it at once, so it asks first.
    ; $input waits for an answer, which a script event may not do - so it
    ; is asked from a timer.
    hadd dccore.sws phase confirm
    hadd dccore.sws question $2-
    .timerdccoreSwAsk -m 1 0 dccore.sw.confirm
    return
  }
  ; a stray "aborted" while nothing of the window's is open: nothing to do
  if ($1 == aborted) && ($istok(idle off caps load,%phase,32)) { return }
  hadd dccore.sws phase idle
  if (%phase == preview) {
    if ($1 == error) { dccore.sw.status No preview: %errs $2- }
    elseif (%errs != $null) { dccore.sw.status The preview left out what the bot refused: %errs }
    return
  }
  if ($1 == ok) {
    ; ok <written> <unchanged> <restart> <message>
    var %said = Saved $2 setting(s) $+ $iif($3 > 0,$chr(32) $+ $chr(40) $+ $3 unchanged $+ $chr(41)) $+ .
    if ($4 != -) { %said = %said Restart the bot to apply $replace($4,$chr(44),$chr(44) $+ $chr(32)) $+ . }
    if ($dccore.sw.s(close)) { dccore.sys Settings: %said | dccore.sw.maybeclose | return }
    ; something written: the bot applies it with a rehash, which may first
    ; wait for transfers, and says SETAPPLIED when it has. Until then the
    ; values just sent are the baseline (a reload now would show the old
    ; ones); the page is reloaded at SETAPPLIED.
    if ($2 > 0) {
      dccore.sw.rebase
      hadd dccore.sws applying $2
      hadd dccore.sws w.applied $calc($ctime + 300)
      dccore.sw.status %said Applying...
      return
    }
    hadd dccore.sws after %said
    dccore.sw.load
    return
  }
  if ($1 == error) {
    hdel dccore.sws close
    dccore.sw.status Not saved - $iif(%errs != $null,%errs,$2-)
    if (%errkey != $null) { dccore.sw.goto %errkey }
    return
  }
  ; aborted
  hdel dccore.sws close
  if (%phase != preview) && ($dccore.sw.s(lockednote) == $null) { dccore.sw.status Nothing was saved. }
  hdel dccore.sws lockednote
}
alias dccore.sw.confirm {
  if (!$dialog(dccore.set)) || ($dccore.sw.s(phase) != confirm) { return }
  var %q = $dccore.sw.s(question)
  ; the wait starts once it is answered: timers run while $input is up,
  ; and a question left open for a minute is not a bot that stopped answering
  if ($input(%q,yq,DCCore - Control Panel)) {
    hadd dccore.sws phase apply
    dccore.sw.wait settings
    hadd dccore.sws lastcommit setcommit confirm
    dccore.send setcommit confirm
    return
  }
  hadd dccore.sws phase aborting
  dccore.sw.wait settings
  dccore.send setabort
}

; ---- the lock --------------------------------------------------------
;
;  A console logged in with the paired token (the script's own login) may
;  read the settings but not change them: setcommit, served / folders /
;  onconnect commit, onconnect resend and the on-connect listing (it holds
;  an X login) answer LOCKED <command> until `unlock <password>`. A refused
;  commit leaves its transaction open on the bot. The window asks for the
;  password once, masked, from a timer; sends `unlock` straight from the
;  prompt (the password goes in no table and no variable that outlives the
;  ask, and is never shown or logged); and on UNLOCKED sends the refused
;  command again. Cancel, or a wrong password, drops the open transaction
;  and keeps the edits. The bot remembers the unlock for the rest of the
;  connection (so does dccore.live sw.unlocked); a new one starts locked.

; LOCKED <command - one or two words> <sentence>
alias dccore.sw.locked {
  var %what = $iif($istok(commit resend,$2,32),$1-2,$1)
  ; the listing: the box says so, with Unlock beside it, rather than ask
  ; for the password just because a page was opened
  if (%what == onconnect) {
    dccore.sw.waited onconnect
    hdel dccore.sws oc.ok
    dccore.sw.put 1501 Locked - press Unlock to see the on-connect commands.
    did -b dccore.set 1501
    dccore.sw.status The on-connect commands may hold a login: the bot shows them once it has the admin password (Unlock).
    return
  }
  if (%what == setcommit) {
    dccore.sw.waited settings
    hadd dccore.sws phase locked
  }
  hadd dccore.sws relock %what
  dccore.sw.status The bot needs the admin password to save this.
  .timerdccoreSwUnlock -m 1 0 dccore.sw.unlockask
}
; Asked from a timer ($input waits for an answer, which a script event may
; not do). Masked (p). No comma in the prompt: it would end the argument.
; The answer goes straight into $dccore.sw.unlockarg, in one nested
; expression, and never into a /var (#1281): a /var - like a command's
; parameters - closes up a run of spaces and drops the ones at either end,
; and a password that has them could log in but never unlock.
alias dccore.sw.unlockask {
  if (!$dialog(dccore.set)) { return }
  dccore.sw.unlocksend $dccore.sw.unlockarg($input(The bot needs its admin password to change settings from this console. It unlocks this connection once and is not kept.,po,DCCore - Unlock))
}
; The password as unlock sends it (#1281). One with a run of spaces, or a
; space at either end, is encoded as the window encodes a value, so it
; survives being a command's parameters; the bot tries what it gets as it
; is, then decoded. Any other password goes as typed, which a bot from
; before this still accepts. $1 is an identifier's argument and keeps its
; spaces; what is returned has no run of them and none at either end.
alias dccore.sw.unlockarg {
  if ($regex($1,/^\x20|\x20$|\x20\x20/)) { return $dccore.sw.enc($1) }
  return $1
}
; $1- is $dccore.sw.unlockarg's answer: nothing when the box was cancelled
; or left empty. Sent, never echoed or kept.
alias dccore.sw.unlocksend {
  if ($1- == $null) { dccore.sw.unlockdrop Not saved: no password was given. | return }
  hadd dccore.sws unlocking 1
  dccore.sw.wait unlock
  dccore.send unlock $1-
}
; The Unlock button beside the on-connect box.
alias dccore.sw.unlockbutton {
  if (!$dccore.sw.can(unlock)) { dccore.sw.status This bot has nothing to unlock. | return }
  if ($dccore.st(sw.unlocked)) { dccore.sw.reask onconnect | return }
  hadd dccore.sws relock onconnect
  .timerdccoreSwUnlock -m 1 0 dccore.sw.unlockask
}
alias dccore.sw.unlocked {
  dccore.sw.waited unlock
  hdel dccore.sws unlocking
  hadd dccore.live sw.unlocked 1
  var %what = $dccore.sw.s(relock)
  hdel dccore.sws relock
  if (%what == $null) || (%what == onconnect) {
    dccore.sw.status Unlocked: the on-connect commands are on their way.
    did -e dccore.set 1501
    dccore.sw.reask onconnect
    return
  }
  dccore.sw.status Unlocked - saving again...
  if (%what == setcommit) {
    hadd dccore.sws phase apply
    dccore.sw.wait settings
    dccore.send $dccore.sw.s(lastcommit)
    return
  }
  dccore.send %what
}
; UNLOCK error <message>: still locked (a third wrong one closes the console)
alias dccore.sw.unlockfailed {
  dccore.sw.waited unlock
  hdel dccore.sws unlocking
  dccore.sw.unlockdrop Not unlocked: $1-
}
; No password, or a wrong one: the transaction left open on the bot is
; dropped, the edits stay here.
alias dccore.sw.unlockdrop {
  var %what = $dccore.sw.s(relock)
  hdel dccore.sws relock
  hadd dccore.sws lockednote 1
  if (%what == setcommit) {
    hadd dccore.sws phase aborting
    dccore.sw.wait settings
    dccore.send setabort
  }
  elseif ($istok(served folders onconnect,$gettok(%what,1,32),32)) && ($gettok(%what,2,32) == commit) { dccore.send $gettok(%what,1,32) abort }
  else { hdel dccore.sws resending.oc }
  dccore.sw.status $1- Your changes are still here.
}

; ---- the settings' controls ------------------------------------------

; $1 a setting: its loaded value into its control, and what that showed
alias dccore.sw.fill {
  var %m = $dccore.sw.m(k. $+ $1), %id = $gettok(%m,1,32), %kind = $gettok(%m,2,32), %f = $gettok(%m,3,32)
  var %raw = $dccore.sw.s(v. $+ $1)
  ; a setting this bot does not have (it is older than the window)
  if (%raw == $null) { did -b dccore.set %id | hadd dccore.sws na. $+ $1 1 | return }
  var %v = $dccore.sw.dec($mid(%raw,2))
  if (%kind == bool) {
    if (%v == true) { did -c dccore.set %id }
    else { did -u dccore.set %id }
  }
  elseif (%kind == tri) { dccore.sw.cb.select %id $iif(%v == true,2,$iif(%v == false,3,1)) }
  ; a value the choices do not have: nothing selected
  elseif (%kind == choice) { dccore.sw.cb.select %id $findtok($dccore.sw.m(ch. $+ $1),%v,1,32) }
  elseif (%kind == colour) { dccore.sw.colourfill %id %v }
  elseif (%kind == chanlist) {
    dccore.sw.lv.clear %id
    var %i = 1
    while (%i <= $numtok(%v,44)) {
      if ($remove($gettok(%v,%i,44),$chr(32)) != $null) { dccore.sw.lv.add %id $dccore.sw.tok($remove($gettok(%v,%i,44),$chr(32))) }
      inc %i
    }
  }
  else {
    if (%f > 1) && (%v isnum) { %v = $calc(%v / %f) }
    dccore.sw.put %id %v
  }
  ; the baseline is what the control shows NOW, read back: did -a may have
  ; closed up a run of spaces, and an untouched field must never be sent
  hadd dccore.sws d. $+ $1 = $+ $dccore.sw.shownenc($1)
}
; $1 = 1 (kept over a reconnect): a control with an edit not saved yet is
; left as it is, with its old baseline, and only the others are refreshed.
alias dccore.sw.fillall {
  var %j = 1
  while (%j <= $dccore.sw.m(keys.n)) {
    var %keys = $dccore.sw.m(keys. $+ %j), %i = 1
    while (%i <= $numtok(%keys,32)) {
      if (!$1) || (!$dccore.sw.dirty($gettok(%keys,%i,32))) { dccore.sw.fill $gettok(%keys,%i,32) }
      inc %i
    }
    inc %j
  }
}
; A colour: "\x03NN" or "\x03NN,NN", or nothing for the theme's own. The
; foreground combo is $1 and the background one $1 + 1; line 1 of each is
; the theme's own colour, line C + 2 is colour C. Anything else - bold, a
; code written by hand - is kept as it is, as an 18th line "As set", until
; a colour is picked over it. (\x2C is a comma, which would end $regex's
; argument.)
alias dccore.sw.colourfill {
  var %fg = 1, %bg = 1, %x
  if ($2 == $null) { %fg = 1 }
  elseif ($regex(dccoresw,$2,/^\\x03(\d\d?)$/)) && ($regml(dccoresw,1) <= 15) { %fg = $calc($regml(dccoresw,1) + 2) }
  elseif ($regex(dccoresw,$2,/^\\x03(\d\d?)\x2C(\d\d?)$/)) && ($regml(dccoresw,1) <= 15) && ($regml(dccoresw,2) <= 15) {
    %fg = $calc($regml(dccoresw,1) + 2)
    %bg = $calc($regml(dccoresw,2) + 2)
  }
  else {
    %x = As set: $2-
    %fg = 18
  }
  ; the 18th line first - taken away, or "As set" - then the selections:
  ; a menu waiting for its lines counts line 18 only once it is kept
  dccore.sw.cb.extra $1 %x
  dccore.sw.cb.select $1 %fg
  dccore.sw.cb.select $calc($1 + 1) %bg
  dccore.sw.colourbg $1
}
; No background without a foreground: "\x03,05" is not a colour.
alias dccore.sw.colourbg {
  var %fg = $dccore.sw.cb.sel($1)
  if (%fg > 1) && (%fg < 18) { did -e dccore.set $calc($1 + 1) }
  else { dccore.sw.cb.select $calc($1 + 1) 1 | did -b dccore.set $calc($1 + 1) }
}

; $1 a setting: what its control shows now, to compare with what it showed
; when the value was loaded
alias dccore.sw.shown {
  var %m = $dccore.sw.m(k. $+ $1), %id = $gettok(%m,1,32), %kind = $gettok(%m,2,32)
  if (%kind == bool) { return $did(dccore.set,%id).state }
  if (%kind == tri) || (%kind == choice) { return $dccore.sw.cb.sel(%id) }
  if (%kind == colour) { return $dccore.sw.cb.sel(%id) $+ / $+ $dccore.sw.cb.sel($calc(%id + 1)) }
  if (%kind == chanlist) { return $dccore.sw.chans }
  return $did(dccore.set,%id).text
}
; What the dirty check compares: shown, with an edit's text encoded - so it
; survives /hadd as it is, runs of spaces and all.
alias dccore.sw.shownenc {
  if ($istok(bool tri choice colour chanlist,$gettok($dccore.sw.m(k. $+ $1),2,32),32)) { return $dccore.sw.shown($1) }
  return $dccore.sw.enc($did(dccore.set,$gettok($dccore.sw.m(k. $+ $1),1,32)).text)
}
alias dccore.sw.dirty {
  if ($dccore.sw.s(v. $+ $1) == $null) { return $false }
  if ($+(=,$dccore.sw.shownenc($1)) === $dccore.sw.s(d. $+ $1)) { return $false }
  return $true
}
; The settings that changed since they were loaded, space separated.
alias dccore.sw.changed {
  var %out, %j = 1
  while (%j <= $dccore.sw.m(keys.n)) {
    var %keys = $dccore.sw.m(keys. $+ %j), %i = 1
    while (%i <= $numtok(%keys,32)) {
      if ($dccore.sw.dirty($gettok(%keys,%i,32))) { %out = %out $gettok(%keys,%i,32) }
      inc %i
    }
    inc %j
  }
  return %out
}
; $1 a setting: what its control says, in its settings.conf form (encoded
; only as it is sent). A size shown in KB or MB goes back in bytes.
alias dccore.sw.value {
  var %m = $dccore.sw.m(k. $+ $1), %id = $gettok(%m,1,32), %kind = $gettok(%m,2,32), %f = $gettok(%m,3,32)
  if (%kind == bool) { return $iif($did(dccore.set,%id).state == 1,true,false) }
  if (%kind == tri) {
    if ($dccore.sw.cb.sel(%id) == 2) { return true }
    if ($dccore.sw.cb.sel(%id) == 3) { return false }
    return
  }
  ; nothing selected (a value the choices do not have): the value as loaded,
  ; never $gettok(...,0,32), which is the number of choices
  if (%kind == choice) {
    if (!$dccore.sw.cb.sel(%id)) { return $dccore.sw.dec($mid($dccore.sw.s(v. $+ $1),2)) }
    return $gettok($dccore.sw.m(ch. $+ $1),$dccore.sw.cb.sel(%id),32)
  }
  if (%kind == colour) {
    var %fg = $dccore.sw.cb.sel(%id), %bg = $dccore.sw.cb.sel($calc(%id + 1))
    if (%fg == 18) { return $dccore.sw.dec($mid($dccore.sw.s(v. $+ $1),2)) }
    if (%fg <= 1) { return }
    var %code = \x03 $+ $base($calc(%fg - 2),10,10,2)
    if (%bg > 1) { %code = %code $+ $chr(44) $+ $base($calc(%bg - 2),10,10,2) }
    return %code
  }
  if (%kind == chanlist) { return $dccore.sw.chans }
  var %text = $did(dccore.set,%id).text
  if (%f > 1) && (%text isnum) { return $round($calc(%text * %f),0) }
  return %text
}
; $1 a setting: what goes after "set <KEY> ", encoded. An edit's text is
; encoded straight from the control - never through a variable or a
; command's parameters, which would close up a run of spaces - and a value
; the controls cannot show (no choice selected, a colour "As set") goes back
; exactly as the bot sent it.
alias dccore.sw.wire {
  var %m = $dccore.sw.m(k. $+ $1), %id = $gettok(%m,1,32), %kind = $gettok(%m,2,32), %f = $gettok(%m,3,32)
  if (%kind == choice) && (!$dccore.sw.cb.sel(%id)) { return $mid($dccore.sw.s(v. $+ $1),2) }
  if (%kind == colour) && ($dccore.sw.cb.sel(%id) == 18) { return $mid($dccore.sw.s(v. $+ $1),2) }
  if ($istok(bool tri choice colour chanlist,%kind,32)) { return $dccore.sw.enc($dccore.sw.value($1)) }
  if (%f > 1) && ($did(dccore.set,%id).text isnum) { return $round($calc($did(dccore.set,%id).text * %f),0) }
  return $dccore.sw.enc($did(dccore.set,%id).text)
}
; The label a setting has in the window, for an error about it.
alias dccore.sw.label {
  var %name = $dccore.sw.m(n. $+ $1)
  return $iif(%name != $null,$dccore.sw.untext(%name),$1)
}

; ---- Apply, OK, Cancel ------------------------------------------------

; Apply ($1 = apply), or OK ($1 = close): everything that changed, each kind
; in a transaction of its own - the on-connect commands (onconnect), the
; lists and bindings (served), this mIRC's own switches (dccore.ini, at
; once) and the settings as one setbegin, a set for each, setcommit: one
; save, one rehash. There are no Save buttons on the pages: Apply is the
; one that saves, and OK closes once every reply has come back ok, at once
; when nothing changed. $2 = asked: the File locations question has been
; answered yes.
alias dccore.sw.apply {
  if ($istok(apply confirm aborting preview locked,$dccore.sw.s(phase),32)) { dccore.sw.status Still waiting for the bot... | return }
  hadd dccore.sws close $iif($1 == close,1,0)
  var %local = $dccore.sw.opt.save, %sent = 0
  if ($dccore.sw.ocdirty) && (!$dccore.sw.s(saving.oc)) { dccore.sw.oc.save | inc %sent }
  if ($dccore.sw.s(srv.dirty)) && (!$dccore.sw.s(saving.srv)) { dccore.sw.srv.save | inc %sent }
  var %keys = $iif($dccore.sw.s(loaded),$dccore.sw.changed)
  if (%keys == $null) {
    if (%sent) { dccore.sw.maybeclose | return }
    if ($1 == close) { dialog -x dccore.set | return }
    dccore.sw.status $iif(%local,Saved this mIRC's own switches.,Nothing has changed.)
    return
  }
  if (!$chat($dccore.bot)) { dccore.sw.status Not connected to the bot: nothing was sent. | return }
  ; a File locations path: asked first, from a timer - $input waits for an
  ; answer, which a dialog event may not do - and Apply runs again on yes
  if ($dccore.sw.risky(%keys)) && ($2 != asked) {
    hadd dccore.sws asking $1
    .timerdccoreSwAsk -m 1 0 dccore.sw.riskyask
    return
  }
  hadd dccore.sws phase apply
  dccore.sw.wait settings
  hadd dccore.sws sent %keys
  hadd dccore.sws lastcommit setcommit
  hdel dccore.sws errs
  hdel dccore.sws errkey
  dccore.send setbegin
  var %i = 1
  while (%i <= $numtok(%keys,32)) {
    dccore.send set $gettok(%keys,%i,32) $dccore.sw.wire($gettok(%keys,%i,32))
    inc %i
  }
  dccore.send setcommit
  dccore.sw.status Saving $numtok(%keys,32) setting(s)...
}
alias dccore.sw.riskyask {
  if (!$dialog(dccore.set)) || ($dccore.sw.s(asking) == $null) { return }
  var %how = $dccore.sw.s(asking)
  hdel dccore.sws asking
  if ($input(Change where the bot keeps its files? A wrong path there can lose a queue or a statistics file.,yq,DCCore - Control Panel)) { dccore.sw.apply %how asked | return }
  hdel dccore.sws close
  dccore.sw.status The settings were not sent.
}
; OK: the window closes once nothing is still being saved - the settings
; (phase), the lists, the on-connect commands - and every reply was ok.
alias dccore.sw.maybeclose {
  if (!$dccore.sw.s(close)) { return }
  if ($dccore.sw.s(saving.srv)) || ($dccore.sw.s(saving.oc)) || ($istok(apply confirm aborting,$dccore.sw.s(phase),32)) { return }
  dialog -x dccore.set
}

; ---- leaving a page with changes not saved ---------------------------
;  A click on a tab or a page while something is not saved asks first:
;  Apply saves everything, so the question is only whether to save now.
;  $input waits for an answer, which a dialog event may not do, so it is
;  asked from a timer, and the window stays on its page until then.

; $1 tab or page, $2 which: as the click meant
alias dccore.sw.go {
  if ($1 == side) && ($2 == $dccore.sw.s(side)) { return }
  if ($1 == tab) && ($2 == $dccore.sw.s(tab)) && ($dccore.sw.s(page)) { return }
  if ($1 == page) && ($2 == $dccore.sw.s(page)) { return }
  if (!$dccore.sw.anydirty) { dccore.sw.goon $1 $2 | return }
  hadd dccore.sws going $1 $2
  dccore.sw.stay
  .timerdccoreSwAsk -m 1 0 dccore.sw.leaveask
}
alias dccore.sw.goon {
  if ($1 == side) { dccore.sw.side $2 }
  elseif ($1 == tab) { dccore.sw.tab $2 }
  else { dccore.sw.page $2 }
}
; The tab and the page shown selected again, after the click elsewhere
alias dccore.sw.stay {
  if ($dccore.sw.s(dcx)) { dccore.sw.dcxshow $dccore.sw.s(page) | return }
  did -f dccore.set $calc(1000 + $dccore.sw.s(tab))
  var %at = $findtok($dccore.sw.m(g. $+ $dccore.sw.s(tab) $+ .pages),$dccore.sw.s(page),1,32)
  if (%at) { did -c dccore.set $calc(1019 + %at) }
}
alias dccore.sw.leaveask {
  if (!$dialog(dccore.set)) || ($dccore.sw.s(going) == $null) { return }
  var %go = $dccore.sw.s(going)
  hdel dccore.sws going
  if ($input(You have changes that are not saved. Save them before leaving this page? Yes saves them and goes on; No stays here.,yq,DCCore - Control Panel)) { dccore.sw.apply apply | dccore.sw.goon %go }
}
; Whether anything in the window is not saved: a setting, one of this
; mIRC's switches, the lists and bindings, the on-connect commands.
alias dccore.sw.anydirty {
  if ($dccore.sw.s(srv.dirty)) || ($dccore.sw.ocdirty) { return $true }
  if ($dccore.sw.s(loaded)) && ($dccore.sw.changed != $null) { return $true }
  if ($dccore.sw.localdirty) { return $true }
  return $dccore.sw.opt.dirty
}
alias dccore.sw.localdirty {
  var %all = $dccore.sw.m(locals), %i = 1
  while (%i <= $numtok(%all,32)) {
    var %o = $gettok(%all,%i,32)
    if ($did(dccore.set,$dccore.sw.m(lo. $+ %o)).state != $iif($dccore.opt(%o),1,0)) { return $true }
    inc %i
  }
  return $false
}
; What Apply sent is the baseline now (SETDONE ok, before SETAPPLIED).
alias dccore.sw.rebase {
  var %keys = $dccore.sw.s(sent), %i = 1
  while (%i <= $numtok(%keys,32)) {
    hadd dccore.sws d. $+ $gettok(%keys,%i,32) = $+ $dccore.sw.shownenc($gettok(%keys,%i,32))
    inc %i
  }
}
; Whether a File locations path is among $1.
alias dccore.sw.risky {
  var %i = 1
  while (%i <= $numtok($1,32)) {
    if ($istok($dccore.sw.m(confirm),$gettok($1,%i,32),32)) { return $true }
    inc %i
  }
  return $false
}
; This mIRC's own switches, saved to dccore.ini at once whatever the bot
; says. How many changed.
alias dccore.sw.savelocal {
  var %all = $dccore.sw.m(locals), %i = 1, %n = 0
  while (%i <= $numtok(%all,32)) {
    var %o = $gettok(%all,%i,32), %state = $did(dccore.set,$dccore.sw.m(lo. $+ %o)).state
    if (%state != $dccore.opt(%o)) { hadd dccore %o %state | inc %n }
    inc %i
  }
  if (%n) { dccore.save }
  return %n
}
; The structured page with edits of its own not saved, or nothing.
alias dccore.sw.unsaved {
  if ($dccore.sw.s(srv.dirty)) { return Lists and Channel bindings }
  if ($dccore.sw.ocdirty) { return IRC Server }
  return
}
alias dccore.sw.ocdirty {
  if (!$dccore.sw.s(oc.ok)) { return $false }
  if ($md5($dccore.sw.oc.text) === $dccore.sw.s(oc.loaded)) { return $false }
  return $true
}
; Whether the snapshot $1 would overwrite edits not saved yet (kept over a
; reconnect): then it is not asked for by itself.
alias dccore.sw.keeps {
  if ($1 == served) || ($1 == folders) { return $iif($dccore.sw.s(srv.dirty),$true,$false) }
  if ($1 == onconnect) { return $dccore.sw.ocdirty }
  return $false
}
alias dccore.sw.closed {
  .timerdccoreSwCaps off
  .timerdccoreSwAsk off
  .timerdccoreSwBan off
  .timerdccoreSwTick off
  .timerdccoreSwAsk off
  .timerdccoreSwUnlock off
  ; a transaction still open on the bot is dropped there too
  if ($chat($dccore.bot)) && ($istok(confirm preview,$dccore.sw.s(phase),32)) { dccore.send setabort }
  ; replies still on their way are not shown as noise in @DCCore
  hadd -u10 dccore.live sw.tail 1
  if ($hget(dccore.sws)) { hfree dccore.sws }
  if ($hget(dccore.swm)) { hfree dccore.swm }
}

; ---- what a click does ------------------------------------------------

alias dccore.sw.click {
  var %id = $1
  if (%id isnum 1001-1007) { dccore.sw.go tab $calc(%id - 1000) | return }
  ; a page button: slot N of the tab shown is its Nth page
  if (%id >= 1020) && (%id < $calc(1020 + $dccore.sw.m(slots))) {
    var %p = $gettok($dccore.sw.m(g. $+ $dccore.sw.s(tab) $+ .pages),$calc(%id - 1019),32)
    if (%p) { dccore.sw.go page %p }
    return
  }
  if (%id == 1014) { dccore.sw.apply apply | return }
  if (%id == 1015) { dccore.sw.apply close | return }
  if (%id == 1017) { dccore.sw.reload | return }
  if ($dccore.sw.m(br. $+ %id) != $null) { dccore.sw.browse %id $calc(%id - 1) $gettok($dccore.sw.m(br. $+ %id),1,32) | return }
  ; Channels
  if (%id == 1522) { dccore.sw.chanadd | return }
  if (%id == 1523) { dccore.sw.chandel | return }
  ; IRC Server: the on-connect commands (Apply saves them)
  if (%id == 1505) { if ($dccore.sw.can(onconnect)) { hadd dccore.sws resending.oc 1 | dccore.sw.status Sending the on-connect commands again... | dccore.send onconnect resend } | return }
  if (%id == 1506) { dccore.sw.unlockbutton | return }
  ; Lists (the table 1540 and its buttons) and Channel bindings (rows 1700 up)
  if (%id == 1551) { dccore.sw.srv.open new | return }
  if (%id == 1554) { if ($dccore.sw.srv.selected != $null) { dccore.sw.srv.open $v1 } | else { dccore.sw.status Select the list to edit first. } | return }
  if (%id == 1555) { dccore.sw.srv.delete | return }
  if (%id == 1557) || (%id == 1567) { dccore.sw.reask served | return }
  if (%id == 1773) { dccore.sw.srv.newchan | return }
  if (%id isnum 1700-1759) { dccore.sw.srv.rowclick %id | return }
  ; Bans & ignores
  if (%id == 1581) { dccore.sw.ban.lift | return }
  if (%id == 1582) { dccore.sw.reask banlist | return }
  if (%id == 1587) { dccore.sw.ban.ignore | return }
  if (%id == 1596) { dccore.sw.ban.unban | return }
  if (%id == 1598) { dccore.sw.ban.add | return }
  ; Appearance, and the mIRC side's pairing
  if (%id == 1610) { dccore.sw.preview | return }
  if (%id == 1683) { dccore.sw.opt.pair | return }
  if (%id == 1684) { dccore.sw.opt.unpair | return }
  ; a setting: its help, and a background colour follows its foreground
  var %k = $dccore.sw.m(i. $+ %id)
  if (%k != $null) {
    dccore.sw.help %k
    if ($gettok($dccore.sw.m(k. $+ %k),2,32) == colour) { dccore.sw.colourbg $gettok($dccore.sw.m(k. $+ %k),1,32) }
    dccore.sw.pending
  }
}
alias dccore.sw.touched {
  var %k = $dccore.sw.m(i. $+ $1)
  if (%k != $null) { dccore.sw.help %k | dccore.sw.pending }
}
alias dccore.sw.pending {
  if ($dccore.sw.s(phase) == idle) && ($dccore.sw.s(loaded)) { dccore.sw.status Not saved yet - Apply or OK saves it. }
}
; The help of the setting under the mouse, as the dashboard shows it on hover.
alias dccore.sw.hover {
  if ($1 == $dccore.sw.s(hover)) { return }
  hadd dccore.sws hover $1
  var %k = $dccore.sw.m(i. $+ $1)
  if (%k != $null) { dccore.sw.help %k }
}
alias dccore.sw.help { dccore.sw.put 1012 $dccore.sw.untext($dccore.sw.m(h. $+ $1)) }

; $1 the button, $2 the edit it fills, $3 dir or file. A path on the bot's
; machine, so only when that is this PC.
alias dccore.sw.browse {
  if (!$dccore.sw.local) { dccore.sw.status The bot runs on another computer: type the path as it is there. | return }
  var %now = $did(dccore.set,$2).text
  var %start = $iif($isdir($nofile(%now)),$nofile(%now),$mircdir)
  if ($3 == dir) {
    var %p = $sdir(%start,Choose a folder)
    if ($right(%p,1) == \) && ($len(%p) > 3) { %p = $left(%p,-1) }
  }
  else { var %p = $sfile(%start,Choose a file,Choose) }
  if (%p != $null) { dccore.sw.put $2 %p | dccore.sw.pending }
}

; ---- Channels: the CHANNEL setting as a list --------------------------

alias dccore.sw.chans {
  var %id = $gettok($dccore.sw.m(k.CHANNEL),1,32), %i = 1, %out
  while (%i <= $dccore.sw.lv.count(%id)) { %out = $addtok(%out,$dccore.sw.lv.text(%id,%i),44) | inc %i }
  return %out
}
alias dccore.sw.chanadd {
  var %c = $remove($did(dccore.set,1521).text,$chr(32),$chr(44))
  if (%c == $null) { dccore.sw.status Type the channel to add first. | return }
  dccore.sw.lv.add 1520 $dccore.sw.tok(%c)
  did -r dccore.set 1521
  dccore.sw.pending
}
alias dccore.sw.chandel {
  var %sel = $dccore.sw.lv.sel(1520)
  if (!%sel) { dccore.sw.status Select the channel to remove first. | return }
  dccore.sw.lv.del 1520 %sel
  dccore.sw.pending
}

; ---- Theme preview ----------------------------------------------------

; The colours as chosen here, not saved: a transaction that is never
; committed - setbegin, the theme's settings, setpreview, setabort.
alias dccore.sw.preview {
  if (!$dccore.sw.s(loaded)) || ($dccore.sw.s(phase) != idle) { dccore.sw.status Wait until the settings have loaded. | return }
  if (!$dccore.sw.can(preview)) { dccore.sw.status This bot has no theme preview - update it. | return }
  hadd dccore.sws phase preview
  dccore.sw.wait settings
  dccore.send setbegin
  dccore.send set THEME $dccore.sw.wire(THEME)
  var %j = 1
  while (%j <= $dccore.sw.m(keys.n)) {
    var %keys = $dccore.sw.m(keys. $+ %j), %i = 1
    while (%i <= $numtok(%keys,32)) {
      if ($gettok($dccore.sw.m(k. $+ $gettok(%keys,%i,32)),2,32) == colour) { dccore.send set $gettok(%keys,%i,32) $dccore.sw.wire($gettok(%keys,%i,32)) }
      inc %i
    }
    inc %j
  }
  dccore.send setpreview
  dccore.send setabort
}

; ---- IRC Server: the on-connect commands ------------------------------

alias dccore.sw.oc.fill {
  did -e dccore.set 1501
  did -r dccore.set 1501
  var %i = 1
  while (%i <= $1) {
    did -a dccore.set 1501 $dccore.sw.dec($mid($dccore.sw.s(oc.l. $+ %i),2)) $+ $iif(%i < $1,$crlf)
    inc %i
  }
  ; what each line shows now, read back and encoded, beside the line as the
  ; bot sent it (oc.l): dccore.sw.oc.wire sends an untouched line back as sent
  hdel -w dccore.sws oc.s.*
  %i = 1
  while (%i <= $did(dccore.set,1501).lines) {
    hadd dccore.sws oc.s. $+ %i $+(=,$dccore.sw.enc($did(dccore.set,1501,%i)))
    inc %i
  }
  dccore.sw.put 1503 $dccore.sw.s(oc.delay)
  hadd dccore.sws oc.ok 1
  hadd dccore.sws oc.loaded $md5($dccore.sw.oc.text)
}
; The commands as typed, one per line, for comparing.
alias dccore.sw.oc.text {
  var %i = 1, %out
  while (%i <= $did(dccore.set,1501).lines) { %out = %out $+ $did(dccore.set,1501,%i) $+ $chr(7) | inc %i }
  return %out $+ $did(dccore.set,1503).text
}
alias dccore.sw.oc.save {
  if (!$dccore.sw.s(oc.ok)) { dccore.sw.status The on-connect commands have not loaded. | return }
  var %delay = $did(dccore.set,1503).text
  if (%delay !isnum) { dccore.sw.status Seconds between commands is a number. | return }
  hadd dccore.sws saving.oc 1
  dccore.send onconnect begin
  dccore.send onconnect delay %delay
  var %i = 1, %n = 0
  while (%i <= $did(dccore.set,1501).lines) {
    if ($remove($did(dccore.set,1501,%i),$chr(32)) != $null) { inc %n | dccore.send onconnect line %n $dccore.sw.oc.wire(%i) }
    inc %i
  }
  dccore.send onconnect commit
  dccore.sw.status Saving %n on-connect command(s)...
}

; $1 a line of the box: the line as the bot sent it when a loaded line shows
; exactly this, else what was typed, encoded straight from the control.
alias dccore.sw.oc.wire {
  var %now = $+(=,$dccore.sw.enc($did(dccore.set,1501,$1))), %k = 1
  while ($dccore.sw.s(oc.s. $+ %k) != $null) {
    if ($dccore.sw.s(oc.s. $+ %k) === %now) { return $mid($dccore.sw.s(oc.l. $+ %k),2) }
    inc %k
  }
  return $mid(%now,2)
}

; ---- Lists, and Channel bindings ------------------------------------
;
;  Two pages, as the mockup draws them. Lists: a table with a row per list
;  (Name | Primary | Folders) and Add list, Edit, Delete and Refresh under
;  it - Add list and Edit open the list window (dccore.setl) for one
;  list's name, primary flag and folders. Channel bindings: a row per
;  channel with its own List and Mode menus and a Remove button (a change
;  in a menu is kept at once), a scrollbar beside the fifteen rows shown
;  for the channels beyond them, and the row that adds one under them.
;  Each list has an id of the window's own
;  (srv.l.<id> = "<primary> <name>", in srv.order), so removing one
;  renumbers nothing; channels and folders name the id of their list
;  (srv.c.<n> = "<list> <channel token> <mode>", srv.f.<n> = "<list>
;  <label token> <path>"). The table's row N is mapped back by srv.rowl.N,
;  a bindings row R by bind.row.R. Names and paths are kept ENCODED - as
;  the bot sent them, or enc() of what was typed, taken straight from the
;  control - and decoded only to be shown, so a row nobody touched goes
;  back byte for byte. Save lists sends the whole set back, in the order
;  the bot sent it, so a set left as it was saves nothing.

; Count a row of the snapshot: $1 is 1 for a list, 2 a channel, 3 a folder.
alias dccore.sw.srv.count {
  var %got = $dccore.sw.s(srv.got)
  hadd dccore.sws srv.got $iif($1 == 1,$calc($gettok(%got,1,32) + 1),$gettok(%got,1,32)) $iif($1 == 2,$calc($gettok(%got,2,32) + 1),$gettok(%got,2,32)) $iif($1 == 3,$calc($gettok(%got,3,32) + 1),$gettok(%got,3,32))
}
; $1 the list's id, $2 the channel as a token, $3 the mode
alias dccore.sw.srv.addchan {
  hinc dccore.sws srv.cnext
  hadd dccore.sws srv.c. $+ $dccore.sw.s(srv.cnext) $1-3
  hadd dccore.sws srv.corder $dccore.sw.s(srv.corder) $dccore.sw.s(srv.cnext)
}
; $1 the list's id, $2 the label as a token, $3- the path
alias dccore.sw.srv.addfolder {
  hinc dccore.sws srv.fnext
  hadd dccore.sws srv.f. $+ $dccore.sw.s(srv.fnext) $1 $2 $3-
  hadd dccore.sws srv.forder $dccore.sw.s(srv.forder) $dccore.sw.s(srv.fnext)
}
; The lists and their channels, without the folders: unchanged, and the
; bot has no lists.json, means the save is the folders command's.
alias dccore.sw.srv.sig {
  var %order = $dccore.sw.s(srv.order), %i = 1, %out
  while (%i <= $numtok(%order,32)) {
    var %id = $gettok(%order,%i,32), %c = $dccore.sw.s(srv.corder), %j = 1
    %out = %out L $dccore.sw.s(srv.l. $+ %id)
    while (%j <= $numtok(%c,32)) {
      if ($gettok($dccore.sw.s(srv.c. $+ $gettok(%c,%j,32)),1,32) == %id) { %out = %out C $gettok($dccore.sw.s(srv.c. $+ $gettok(%c,%j,32)),2-,32) }
      inc %j
    }
    inc %i
  }
  return %out
}
; FLDEND: the implied list's folders are the served folders.
alias dccore.sw.srv.implied {
  if ($dccore.sw.s(srv.src) != implied) || ($numtok($dccore.sw.s(srv.order),32) != 1) { return }
  var %id = $dccore.sw.s(srv.order), %f = $dccore.sw.s(srv.forder), %i = 1
  while (%i <= $numtok(%f,32)) { hdel dccore.sws srv.f. $+ $gettok(%f,%i,32) | inc %i }
  hdel dccore.sws srv.forder
  %i = 1
  while (%i <= $dccore.sw.s(fld.want)) { dccore.sw.srv.addfolder %id $dccore.sw.s(fld.r. $+ %i) | inc %i }
  dccore.sw.srv.draw
  dccore.sw.status No lists.json yet: one list over the served folders $+ $iif($dccore.sw.s(fld.src) == file_directory,$chr(32) $+ $chr(40) $+ the music directory $+ $chr(41)) $+ . Editing anything but its folders makes the list real.
}
alias dccore.sw.srv.listname { return $dccore.sw.dec($gettok($dccore.sw.s(srv.l. $+ $1),2-,32)) }
; a mode this window does not know (a newer bot) is shown as it is
alias dccore.sw.srv.modelabel {
  var %at = $findtok($dccore.sw.m(modes),$1,1,32)
  if (!%at) { return $1 }
  return $gettok($dccore.sw.untext($dccore.sw.m(modelabels)),%at,44)
}
alias dccore.sw.srv.draw {
  dccore.sw.lv.clear 1540
  hdel -w dccore.sws srv.rowl.*
  var %order = $dccore.sw.s(srv.order), %f = $dccore.sw.s(srv.forder), %i = 1
  while (%i <= $numtok(%order,32)) {
    var %id = $gettok(%order,%i,32), %l = $dccore.sw.s(srv.l. $+ %id), %j = 1, %folders
    while (%j <= $numtok(%f,32)) {
      var %row = $dccore.sw.s(srv.f. $+ $gettok(%f,%j,32))
      if ($gettok(%row,1,32) == %id) { %folders = %folders $+ $iif(%folders != $null,$+($chr(59),$chr(32))) $+ $dccore.sw.untok($gettok(%row,2,32)) = $dccore.sw.dec($gettok(%row,3-,32)) }
      inc %j
    }
    dccore.sw.lv.add 1540 $iif($gettok(%l,2-,32) != $null,$dccore.sw.retok($gettok(%l,2-,32)),$dccore.sw.tok($chr(40) $+ unnamed $+ $chr(41))) $iif($gettok(%l,1,32) == 1,$chr(10003),-) $dccore.sw.tok(%folders)
    hadd dccore.sws srv.rowl. $+ %i %id
    inc %i
  }
  dccore.sw.srv.drawrows
}
; The lists table's selected row: its list's id, or nothing.
alias dccore.sw.srv.selected { return $dccore.sw.s(srv.rowl. $+ $dccore.sw.lv.sel(1540)) }
; The mode chosen in the Mode menu $1: the first when none is.
alias dccore.sw.srv.mode { return $gettok($dccore.sw.m(modes),$iif($did(dccore.set,$1).sel,$did(dccore.set,$1).sel,1),32) }
; The list chosen in the List menu $1, as an id: the first when none is.
alias dccore.sw.srv.chosen { return $gettok($dccore.sw.s(srv.order),$iif($did(dccore.set,$1).sel,$did(dccore.set,$1).sel,1),32) }
; Select line $2 of combo $1 - or none, when $findtok found nothing (0).
alias dccore.sw.pickline {
  if ($2) { did -c dccore.set $1 $2 }
  else { did -u dccore.set $1 }
}
alias dccore.sw.srv.ready {
  if ($dccore.sw.s(srv.ok)) { return $true }
  dccore.sw.status The lists have not loaded: press Refresh.
  return $false
}
; Only one list is primary: ticking one unticks the others.
alias dccore.sw.srv.oneprimary {
  var %order = $dccore.sw.s(srv.order), %i = 1
  while (%i <= $numtok(%order,32)) {
    var %id = $gettok(%order,%i,32)
    if (%id != $1) { hadd dccore.sws srv.l. $+ %id 0 $gettok($dccore.sw.s(srv.l. $+ %id),2-,32) }
    inc %i
  }
}
; $1 a List menu: the lists' names into it, in srv.order
alias dccore.sw.srv.listsinto {
  did -r dccore.set $1
  var %order = $dccore.sw.s(srv.order), %i = 1
  while (%i <= $numtok(%order,32)) {
    var %l = $dccore.sw.s(srv.l. $+ $gettok(%order,%i,32))
    did -a dccore.set $1 $iif($gettok(%l,2-,32) != $null,$dccore.sw.srv.listname($gettok(%order,%i,32)),$chr(40) $+ unnamed $+ $chr(41))
    inc %i
  }
}
; Delete: the selected list goes, with its channels and folders.
alias dccore.sw.srv.delete {
  if (!$dccore.sw.srv.ready) { return }
  var %n = $dccore.sw.srv.selected
  if (%n == $null) { dccore.sw.status Select the list to delete first. | return }
  hdel dccore.sws srv.l. $+ %n
  dccore.sw.keep srv.order $remtok($dccore.sw.s(srv.order),%n,1,32)
  var %c = $dccore.sw.s(srv.corder), %i = $numtok(%c,32)
  while (%i >= 1) {
    if ($gettok($dccore.sw.s(srv.c. $+ $gettok(%c,%i,32)),1,32) == %n) { hdel dccore.sws srv.c. $+ $gettok(%c,%i,32) | %c = $deltok(%c,%i,32) }
    dec %i
  }
  dccore.sw.keep srv.corder %c
  var %f = $dccore.sw.s(srv.forder)
  %i = $numtok(%f,32)
  while (%i >= 1) {
    if ($gettok($dccore.sw.s(srv.f. $+ $gettok(%f,%i,32)),1,32) == %n) { hdel dccore.sws srv.f. $+ $gettok(%f,%i,32) | %f = $deltok(%f,%i,32) }
    dec %i
  }
  dccore.sw.keep srv.forder %f
  dccore.sw.srv.edited
}

; -- Channel bindings: the mockup's rows ----------------------------------
;  Row R of the fifteen: its channel is 1700 + 4(R-1), its List menu +1,
;  its Mode menu +2, its Remove +3; the scrollbar beside them is 1774, the
;  row that adds one 1770 to 1773, and the count sits beside Save lists
;  (1568). The rows never move: the scrollbar's position is bind.off, the
;  first binding shown, and the rows are filled from it - bind.row.R is
;  the binding (its number in srv.corder) row R holds.
alias dccore.sw.srv.rowbase { return $calc(1700 + 4 * ($1 - 1)) }
; The rows from bind.off: a row each, the spare rows hidden, the scrollbar
; ranged over what is beyond the fifteen
alias dccore.sw.srv.drawrows {
  var %c = $dccore.sw.s(srv.corder), %n = $numtok(%c,32), %off = $dccore.sw.s(bind.off), %r = 1
  if (%off == $null) { %off = 0 }
  if (%off > $calc(%n - 15)) { %off = $calc(%n - 15) }
  if (%off < 0) { %off = 0 }
  hadd dccore.sws bind.off %off
  did -z dccore.set 1774 range 0 $iif(%n > 15,$calc(%n - 15),0)
  did -c dccore.set 1774 %off
  did $iif(%n > 15,-e,-b) dccore.set 1774
  hdel -w dccore.sws bind.row.*
  while (%r <= 15) {
    var %k = $calc(%off + %r), %base = $dccore.sw.srv.rowbase(%r)
    if (%k <= %n) {
      var %row = $dccore.sw.s(srv.c. $+ $gettok(%c,%k,32))
      hadd dccore.sws bind.row. $+ %r $gettok(%c,%k,32)
      dccore.sw.put %base $dccore.sw.untok($gettok(%row,2,32))
      dccore.sw.srv.listsinto $calc(%base + 1)
      dccore.sw.pickline $calc(%base + 1) $findtok($dccore.sw.s(srv.order),$gettok(%row,1,32),1,32)
      dccore.sw.pickline $calc(%base + 2) $findtok($dccore.sw.m(modes),$gettok(%row,3,32),1,32)
    }
    inc %r
  }
  ; the row that adds one offers the first list and the first mode, as the mockup does
  dccore.sw.srv.listsinto 1771
  dccore.sw.pickline 1771 $iif($dccore.sw.s(srv.order) != $null,1,0)
  if (!$did(dccore.set,1772).sel) { did -c dccore.set 1772 1 }
  dccore.sw.srv.rowsvis
  dccore.sw.put 1568 $iif(%n,%n channel binding $+ $iif(%n != 1,s),No channel bindings yet)
}
; The scrollbar moved: the rows filled from its position
on *:dialog:dccore.set:scroll:*: { dccore.sw.scrolled $did }
alias dccore.sw.scrolled {
  if ($1 != 1774) { return }
  hadd dccore.sws bind.off $did(dccore.set,1774).sel
  dccore.sw.srv.drawrows
}
; The rows with a binding shown, the rest hidden - again each time the
; page is shown, since showing a page shows every control of it. Only
; while that page IS the one shown: the lists arrive from the bot a moment
; after they were asked for, by which time another page may be up, and a
; row shown then sits on top of it.
alias dccore.sw.srv.rowsvis {
  if (!$istok($dccore.sw.m(p. $+ $dccore.sw.s(page) $+ .w),srvchans,32)) { return }
  var %r = 1, %n = $numtok($dccore.sw.s(srv.corder),32), %off = $dccore.sw.s(bind.off)
  if (%off == $null) { %off = 0 }
  while (%r <= 15) {
    var %base = $dccore.sw.srv.rowbase(%r)
    did $iif($calc(%off + %r) <= %n,-v,-h) dccore.set $+(%base,$chr(44),$calc(%base + 1),$chr(44),$calc(%base + 2),$chr(44),$calc(%base + 3))
    inc %r
  }
}
; A click in a row ($1 the control): its List or Mode menu changed, kept
; at once, or its Remove pressed
alias dccore.sw.srv.rowclick {
  var %r = $calc($int($calc(($1 - 1700) / 4)) + 1), %what = $calc(($1 - 1700) - 4 * $int($calc(($1 - 1700) / 4))), %base = $dccore.sw.srv.rowbase(%r)
  var %n = $dccore.sw.s(bind.row. $+ %r)
  if (%n == $null) || (!$dccore.sw.srv.ready) { return }
  if (%what == 3) {
    hdel dccore.sws srv.c. $+ %n
    dccore.sw.keep srv.corder $remtok($dccore.sw.s(srv.corder),%n,1,32)
    dccore.sw.srv.edited
    return
  }
  if (%what == 1) || (%what == 2) {
    var %row = $dccore.sw.s(srv.c. $+ %n)
    hadd dccore.sws srv.c. $+ %n $dccore.sw.srv.chosen($calc(%base + 1)) $gettok(%row,2,32) $dccore.sw.srv.mode($calc(%base + 2))
    hadd dccore.sws srv.dirty 1
    dccore.sw.status $dccore.sw.untok($gettok(%row,2,32)) changed - Apply or OK sends the lists and bindings to the bot.
  }
}
; The row that adds one: the channel typed, the list and mode chosen.
alias dccore.sw.srv.newchan {
  if (!$dccore.sw.srv.ready) { return }
  var %chan = $remove($did(dccore.set,1770).text,$chr(32))
  if (%chan == $null) || ($dccore.sw.s(srv.order) == $null) { dccore.sw.status Type the channel first, and have a list for it. | return }
  dccore.sw.srv.addchan $dccore.sw.srv.chosen(1771) $dccore.sw.tok(%chan) $dccore.sw.srv.mode(1772)
  did -r dccore.set 1770
  ; scrolled to the end, where the new row is
  hadd dccore.sws bind.off $calc($numtok($dccore.sw.s(srv.corder),32) - 15)
  dccore.sw.srv.edited
}
alias dccore.sw.srv.edited {
  hadd dccore.sws srv.dirty 1
  dccore.sw.srv.draw
  dccore.sw.status Lists or bindings changed - Apply or OK sends the whole set to the bot.
}
; The whole set back to the bot. With no lists.json and only the folders
; changed, through the folders command, which does not make the implied
; list a real one.
alias dccore.sw.srv.save {
  if (!$dccore.sw.srv.ready) { return }
  var %order = $dccore.sw.s(srv.order), %f = $dccore.sw.s(srv.forder), %c = $dccore.sw.s(srv.corder)
  hdel dccore.sws srv.errs
  hadd dccore.sws saving.srv 1
  ; === : a rename that changes only the case is a change (== ignores case)
  if ($dccore.sw.s(srv.src) == implied) && ($numtok(%order,32) == 1) && ($dccore.sw.srv.sig === $dccore.sw.s(srv.sig0)) && ($dccore.sw.can(folders)) {
    dccore.send folders begin
    var %i = 1, %n = 0
    while (%i <= $numtok(%f,32)) {
      var %row = $dccore.sw.s(srv.f. $+ $gettok(%f,%i,32))
      inc %n
      dccore.send folders row %n $gettok(%row,2-,32)
      inc %i
    }
    dccore.send folders commit
    dccore.sw.status Saving the served folders...
    return
  }
  dccore.send served begin
  var %i = 1
  while (%i <= $numtok(%order,32)) {
    var %id = $gettok(%order,%i,32), %j = 1
    var %l = $dccore.sw.s(srv.l. $+ %id)
    dccore.send served list %i %l
    while (%j <= $numtok(%c,32)) {
      var %row = $dccore.sw.s(srv.c. $+ $gettok(%c,%j,32))
      if ($gettok(%row,1,32) == %id) { dccore.send served chan %i $gettok(%row,2-3,32) }
      inc %j
    }
    %j = 1
    while (%j <= $numtok(%f,32)) {
      var %row = $dccore.sw.s(srv.f. $+ $gettok(%f,%j,32))
      if ($gettok(%row,1,32) == %id) { dccore.send served folder %i $gettok(%row,2-,32) }
      inc %j
    }
    inc %i
  }
  dccore.send served commit
  dccore.sw.status Saving the lists...
}

; -- the list window: Add list and Edit ----------------------------------
;  One list's name, primary flag and folders, in dccore.swl while it is
;  open: id (the list's id, or "new"), name (encoded), primary, f.N =
;  "<label token> <path>" in forder. OK writes them into the set, Cancel
;  drops them; nothing reaches the bot until Save lists. Its folders are
;  typed here and kept, as the set's are, tok() or enc() of the control.
dialog dccore.setl {
  title "DCCore - List"
  size -1 -1 300 168
  option dbu
  text "Name", 1, 6 8 30 8
  edit "", 2, 40 6 150 11, autohs
  check "Primary: answers a private message, and any channel bound to no list", 3, 6 22 288 10
  box "Folders", 4, 6 36 288 106
  list 5, 12 46 276 42, hsbar vsbar
  text "Label", 6, 12 94 24 8
  edit "", 7, 38 92 100 11, autohs
  text "Path", 8, 144 94 20 8
  edit "", 9, 166 92 104 11, autohs
  button "...", 10, 272 91 16 13
  button "Add folder", 11, 12 108 52 13
  button "Change", 12, 67 108 46 13
  button "Remove", 13, 116 108 46 13
  text "", 14, 12 124 276 16
  button "OK", 15, 196 150 46 13
  button "Cancel", 16, 246 150 46 13, cancel
}
on *:dialog:dccore.setl:init:0: { dccore.sw.srv.linit | dccore.theme.dialog dccore.setl init }
on *:dialog:dccore.setl:sclick:*: { dccore.sw.srv.lclick $did }
; $1 a list's id, or "new"
alias dccore.sw.srv.open {
  if (!$dccore.sw.srv.ready) { return }
  if ($dialog(dccore.setl)) { dialog -v dccore.setl | return }
  if ($1 == new) && ($numtok($dccore.sw.s(srv.order),32) >= $dccore.sw.s(srv.max)) { dccore.sw.status A bot serves at most $dccore.sw.s(srv.max) lists. | return }
  if ($hget(dccore.swl)) { hfree dccore.swl }
  hmake dccore.swl 20
  hadd dccore.swl id $1
  hadd dccore.swl fnext 0
  if ($1 != new) {
    var %l = $dccore.sw.s(srv.l. $+ $1)
    hadd dccore.swl primary $gettok(%l,1,32)
    if ($gettok(%l,2-,32) != $null) { hadd dccore.swl name $gettok(%l,2-,32) }
    var %f = $dccore.sw.s(srv.forder), %i = 1
    while (%i <= $numtok(%f,32)) {
      var %row = $dccore.sw.s(srv.f. $+ $gettok(%f,%i,32))
      if ($gettok(%row,1,32) == $1) { dccore.sw.srv.lfolder $gettok(%row,2-,32) }
      inc %i
    }
  }
  dialog -m dccore.setl dccore.setl
}
; $1 a label token, $2- a path (encoded): a folder of the window's list
alias dccore.sw.srv.lfolder {
  hinc dccore.swl fnext
  hadd dccore.swl f. $+ $hget(dccore.swl,fnext) $1-
  hadd dccore.swl forder $hget(dccore.swl,forder) $hget(dccore.swl,fnext)
}
alias dccore.sw.srv.linit {
  dialog -t dccore.setl $iif($hget(dccore.swl,id) == new,DCCore - New list,DCCore - Edit list)
  dccore.sw.lput 2 $dccore.sw.dec($hget(dccore.swl,name))
  if ($hget(dccore.swl,primary) == 1) { did -c dccore.setl 3 }
  if (!$dccore.sw.local) { did -b dccore.setl 10 }
  dccore.sw.srv.ldraw
}
; Put a text into a control of the list window: did -a refuses an empty one.
alias dccore.sw.lput {
  did -r dccore.setl $1
  if ($2- != $null) { did -a dccore.setl $1 $2- }
}
alias dccore.sw.srv.ldraw {
  did -r dccore.setl 5
  var %f = $hget(dccore.swl,forder), %i = 1
  while (%i <= $numtok(%f,32)) {
    var %row = $hget(dccore.swl,f. $+ $gettok(%f,%i,32))
    did -a dccore.setl 5 $dccore.sw.untok($gettok(%row,1,32)) = $dccore.sw.dec($gettok(%row,2-,32))
    inc %i
  }
}
; The folder row selected: its number in forder, or nothing.
alias dccore.sw.srv.lsel { return $gettok($hget(dccore.swl,forder),$did(dccore.setl,5).sel,32) }
alias dccore.sw.srv.lclick {
  if ($1 == 5) {
    var %row = $hget(dccore.swl,f. $+ $dccore.sw.srv.lsel)
    if (%row != $null) { dccore.sw.lput 7 $dccore.sw.untok($gettok(%row,1,32)) | dccore.sw.lput 9 $dccore.sw.dec($gettok(%row,2-,32)) }
    return
  }
  if ($1 == 10) { dccore.sw.srv.lbrowse | return }
  if ($1 == 11) {
    if ($did(dccore.setl,9).text == $null) { did -ra dccore.setl 14 Type the folder's path first; Label is what the list calls it. | return }
    dccore.sw.srv.lfolder $dccore.sw.tok($did(dccore.setl,7).text) $dccore.sw.enc($did(dccore.setl,9).text)
    dccore.sw.srv.ldraw
    return
  }
  if ($1 == 12) {
    var %n = $dccore.sw.srv.lsel
    if (%n == $null) { did -ra dccore.setl 14 Select the folder to change first. | return }
    if ($did(dccore.setl,9).text == $null) { did -ra dccore.setl 14 A folder needs a path. | return }
    hadd dccore.swl f. $+ %n $dccore.sw.tok($did(dccore.setl,7).text) $dccore.sw.enc($did(dccore.setl,9).text)
    dccore.sw.srv.ldraw
    return
  }
  if ($1 == 13) {
    var %n = $dccore.sw.srv.lsel
    if (%n == $null) { did -ra dccore.setl 14 Select the folder to remove first. | return }
    hdel dccore.swl f. $+ %n
    var %o = $remtok($hget(dccore.swl,forder),%n,1,32)
    if (%o == $null) { hdel dccore.swl forder }
    else { hadd dccore.swl forder %o }
    dccore.sw.srv.ldraw
    return
  }
  if ($1 == 15) { dccore.sw.srv.lok }
}
; OK: the window's list into the set - a new id for a new list - its old
; folders out and the window's in, then the tables drawn again
alias dccore.sw.srv.lok {
  if ($did(dccore.setl,2).text == $null) { did -ra dccore.setl 14 A list needs a name. | return }
  if (!$dccore.sw.srv.ready) { dialog -x dccore.setl | return }
  var %id = $hget(dccore.swl,id)
  if (%id == new) {
    hinc dccore.sws srv.next
    %id = $dccore.sw.s(srv.next)
    hadd dccore.sws srv.order $dccore.sw.s(srv.order) %id
  }
  hadd dccore.sws srv.l. $+ %id $did(dccore.setl,3).state $dccore.sw.enc($did(dccore.setl,2).text)
  if ($did(dccore.setl,3).state) { dccore.sw.srv.oneprimary %id }
  var %f = $dccore.sw.s(srv.forder), %i = $numtok(%f,32)
  while (%i >= 1) {
    if ($gettok($dccore.sw.s(srv.f. $+ $gettok(%f,%i,32)),1,32) == %id) { hdel dccore.sws srv.f. $+ $gettok(%f,%i,32) | %f = $deltok(%f,%i,32) }
    dec %i
  }
  dccore.sw.keep srv.forder %f
  var %w = $hget(dccore.swl,forder)
  %i = 1
  while (%i <= $numtok(%w,32)) { dccore.sw.srv.addfolder %id $hget(dccore.swl,f. $+ $gettok(%w,%i,32)) | inc %i }
  dialog -x dccore.setl
  dccore.sw.srv.edited
}
; A folder on the bot's machine, so only when that is this PC.
alias dccore.sw.srv.lbrowse {
  if (!$dccore.sw.local) { did -ra dccore.setl 14 The bot runs on another computer: type the path as it is there. | return }
  var %now = $did(dccore.setl,9).text
  var %start = $iif($isdir($nofile(%now)),$nofile(%now),$mircdir)
  var %p = $sdir(%start,Choose a folder)
  if ($right(%p,1) == \) && ($len(%p) > 3) { %p = $left(%p,-1) }
  if (%p != $null) { dccore.sw.lput 9 %p }
}

; ---- Bans & ignores ---------------------------------------------------

; $1 permanent, $2 timed: the true totals, of which at most 200 each came
alias dccore.sw.ban.draw {
  dccore.sw.lv.clear 1580
  dccore.sw.lv.clear 1595
  var %i = 1
  while (%i <= $dccore.sw.s(ban.gt)) {
    var %row = $dccore.sw.s(ban.t. $+ %i)
    dccore.sw.lv.add 1580 $dccore.sw.tok($gettok(%row,2,32)) $dccore.sw.tok($duration($gettok(%row,1,32)) left)
    inc %i
  }
  %i = 1
  while (%i <= $dccore.sw.s(ban.gp)) { dccore.sw.lv.add 1595 $dccore.sw.tok($dccore.sw.s(ban.p. $+ %i)) | inc %i }
  dccore.sw.status $1 permanent pattern(s) and $2 timed ban(s) or ignore(s) $+ $iif(($1 > 200) || ($2 > 200),$chr(32) $+ - the first 200 of each are shown) $+ .
}
; ban, unban, ignore and unignore report in @DCCore and some finish in the
; background, so the list is asked for again a moment later.
alias dccore.sw.ban.after { .timerdccoreSwBan 1 2 dccore.sw.reask banlist }
alias dccore.sw.ban.lift {
  var %sel = $dccore.sw.lv.sel(1580)
  if (!%sel) { dccore.sw.status Select the nick to lift first. | return }
  dccore.send unignore $gettok($dccore.sw.s(ban.t. $+ %sel),2,32)
  dccore.sw.ban.after
}
alias dccore.sw.ban.ignore {
  var %nick = $did(dccore.set,1584).text, %m = $did(dccore.set,1586).text
  if (%nick == $null) || (%m !isnum 1-10080) || (. isin %m) { dccore.sw.status Type a nick and a whole number of minutes from 1 to 10080. | return }
  dccore.send ignore %nick %m
  dccore.sw.ban.after
}
alias dccore.sw.ban.unban {
  var %sel = $dccore.sw.lv.sel(1595)
  if (!%sel) { dccore.sw.status Select the pattern to remove first. | return }
  dccore.send unban $dccore.sw.s(ban.p. $+ %sel)
  dccore.sw.ban.after
}
alias dccore.sw.ban.add {
  var %p = $did(dccore.set,1597).text
  if (%p == $null) { dccore.sw.status Type the pattern first - for example *!*@host.example | return }
  dccore.send ban %p
  did -r dccore.set 1597
  dccore.sw.ban.after
}

; ==== BEGIN GENERATED settings window (scripts/mirc/build_settings_window.py) ====
; Generated - do not edit by hand. Change scripts/mirc/settings_window_layout.py (or the
; bot's settings metadata) and run: python scripts/mirc/build_settings_window.py
dialog dccore.set {
  title "DCCore - Control Panel"
  size -1 -1 488 364
  option dbu
  tab "General", 1001, 4 2 480 338
  tab "Sharing", 1002
  tab "Downloads", 1003
  tab "Security", 1004
  tab "Dashboard && Console", 1005
  tab "Advanced", 1006
  tab "mIRC", 1007
  radio "", 1020, 10 24 88 15, push group
  radio "", 1021, 10 41 88 15, push
  radio "", 1022, 10 58 88 15, push
  radio "", 1023, 10 75 88 15, push
  radio "", 1024, 10 92 88 15, push
  radio "", 1025, 10 109 88 15, push
  text "", 1012, 110 314 370 24
  text "", 1013, 10 342 310 8
  text "", 1018, 10 351 310 10
  button "Reload", 1017, 331 346 36 13
  button "Apply", 1014, 370 346 36 13
  button "OK", 1015, 409 346 36 13
  button "Cancel", 1016, 448 346 36 13, cancel
  box "Connection", 1100, 110 38 370 106
  text "IRC server", 2000, 116 48 358 8
  edit "", 2001, 116 57 358 11, autohs
  text "Port", 2004, 116 71 358 8
  edit "", 2005, 116 80 358 11, autohs
  text "Nickname", 2008, 116 94 358 8
  edit "", 2009, 116 103 358 11, autohs
  text "Alt nickname", 2012, 116 117 358 8
  edit "", 2013, 116 126 358 11, autohs
  box "Reconnection", 1101, 110 148 370 60
  text "Rejoin attempts after a kick (0 = never)", 2016, 116 158 358 8
  edit "", 2017, 116 167 358 11, autohs
  text "Check the on-connect commands worked every (minutes, 0 = never)", 2020, 116 181 358 8
  edit "", 2021, 116 190 358 11, autohs
  box "On connect", 1102, 110 212 370 91
  text "", 1500, 116 222 358 16
  edit "", 1501, 116 240 358 40, multi return autohs autovs hsbar vsbar
  text "Seconds between commands", 1502, 116 285 120 8
  edit "", 1503, 240 283 24 11, autohs
  button "Unlock", 1506, 272 282 44 13
  button "Resend now", 1505, 324 282 56 13
  text "The on/off switches used most, here from their own pages so there is one place to check.", 1103, 110 38 370 8
  box "Open when mIRC starts (minimised)", 1104, 110 53 370 47
  check "DCCore window", 2025, 116 63 358 10
  check "Chat", 2029, 116 74 358 10
  check "Downloads", 2033, 116 85 358 10
  box "Connection", 1105, 110 104 370 25
  check "Reconnect and log in to the bot by itself when it comes back", 2037, 116 114 358 10
  box "Sharing && search", 1106, 110 133 370 36
  check "Answer @find searches", 2041, 116 143 358 10
  check "Announce finished transfers in the channel", 2045, 116 154 358 10
  box "Messaging && dashboard", 1107, 110 173 370 47
  check "Keep private messages (off: keep none, reply once instead)", 2049, 116 183 358 10
  check "Enable web dashboard", 2053, 116 194 358 10
  check "Answer CTCP VERSION", 2057, 116 205 358 10
  box "Updates", 1108, 110 224 370 25
  check "Tell me when a new version is out", 2061, 116 234 358 10
  box "Admin channels", 1109, 110 38 370 56
  text "A separate channel the bot sends its own diagnostic lines to - never one it also serves files in.", 1110, 116 48 358 16
  text "Debug channel", 2064, 116 67 358 8
  edit "", 2065, 116 76 358 11, autohs
  box "Serving channels", 1111, 110 98 370 213
  text "Which channels the bot joins and sits in. What each one serves - which list, which folders, Normal, Silent or Request only - is set under Sharing > Lists and Channel bindings.", 1112, 116 108 358 24
  list 1520, 116 135 250 168, vsbar
  edit "", 1521, 372 135 102 11, autohs
  button "Add channel", 1522, 372 149 56 13
  button "Remove", 1523, 372 165 56 13
  box "Admin", 1113, 110 38 370 37
  text "Admin nick(s)", 2068, 116 48 358 8
  edit "", 2069, 116 57 358 11, autohs
  box "What the channel sees", 1114, 110 38 370 57
  text "Colour theme", 2072, 116 48 358 8
  combo 2073, 116 57 120 80, drop
  button "Preview", 1610, 116 71 50 13
  text "Draws the sample advert and notice in @DCCore-preview with the colours chosen here, before they are saved.", 1611, 172 71 302 16
  box "Custom override - surface colours", 1115, 110 99 370 102
  text "Set any of these and it replaces that one role in the theme above. Keep previous keeps whatever background the segment before left.", 1116, 116 109 358 16
  text "Border colour", 2076, 116 128 358 8
  combo 2077, 116 137 80 120, drop
  combo 2078, 200 137 80 120, drop
  text "Separator colour", 2080, 116 151 358 8
  combo 2081, 116 160 80 120, drop
  combo 2082, 200 160 80 120, drop
  text "Text box colour", 2084, 116 174 358 8
  combo 2085, 116 183 80 120, drop
  combo 2086, 200 183 80 120, drop
  box "Custom override - text colours", 1117, 110 205 370 83
  text "Value colour", 2088, 116 215 358 8
  combo 2089, 116 224 80 120, drop
  combo 2090, 200 224 80 120, drop
  text "Alert colour", 2092, 116 238 358 8
  combo 2093, 116 247 80 120, drop
  combo 2094, 200 247 80 120, drop
  text "Accent colour", 2096, 116 261 358 8
  combo 2097, 116 270 80 120, drop
  combo 2098, 200 270 80 120, drop
  box "Advert", 1118, 110 38 370 37
  text "Advert interval (seconds)", 2100, 116 48 358 8
  edit "", 2101, 116 57 358 11, autohs
  box "Broadcast search", 1119, 110 79 370 60
  text "Broadcast search channel", 2104, 116 89 358 8
  edit "", 2105, 116 98 358 11, autohs
  text "Broadcast search cooldown (seconds)", 2108, 116 112 358 8
  edit "", 2109, 116 121 358 11, autohs
  box "Timing", 1120, 110 143 370 60
  text "Message delay (seconds)", 2112, 116 153 358 8
  edit "", 2113, 116 162 358 11, autohs
  text "Debug message delay (seconds)", 2116, 116 176 358 8
  edit "", 2117, 116 185 358 11, autohs
  text "What each list shares: a name, its folders, and whether it is primary (it answers a private message, and any channel bound to none). Add list and Edit open a window for one list; Apply sends the whole set to the bot.", 1121, 110 38 370 24
  box "Lists", 1122, 110 69 370 99
  list 1540, 116 79 358 64, hsbar vsbar
  button "Add list", 1551, 116 147 50 13
  button "Edit", 1554, 169 147 46 13
  button "Delete", 1555, 218 147 46 13
  button "Refresh", 1557, 267 147 46 13
  text "Which list answers in each channel, and that channel's mode. A change in a menu is kept at once; Apply sends the set to the bot.", 1123, 110 38 370 16
  box "Channel bindings", 1124, 110 61 370 229
  text "", 1700, 116 73 98 8
  combo 1701, 218 71 84 80, drop
  combo 1702, 306 71 84 60, drop
  button "Remove", 1703, 400 70 56 13
  text "", 1704, 116 86 98 8
  combo 1705, 218 84 84 80, drop
  combo 1706, 306 84 84 60, drop
  button "Remove", 1707, 400 83 56 13
  text "", 1708, 116 99 98 8
  combo 1709, 218 97 84 80, drop
  combo 1710, 306 97 84 60, drop
  button "Remove", 1711, 400 96 56 13
  text "", 1712, 116 112 98 8
  combo 1713, 218 110 84 80, drop
  combo 1714, 306 110 84 60, drop
  button "Remove", 1715, 400 109 56 13
  text "", 1716, 116 125 98 8
  combo 1717, 218 123 84 80, drop
  combo 1718, 306 123 84 60, drop
  button "Remove", 1719, 400 122 56 13
  text "", 1720, 116 138 98 8
  combo 1721, 218 136 84 80, drop
  combo 1722, 306 136 84 60, drop
  button "Remove", 1723, 400 135 56 13
  text "", 1724, 116 151 98 8
  combo 1725, 218 149 84 80, drop
  combo 1726, 306 149 84 60, drop
  button "Remove", 1727, 400 148 56 13
  text "", 1728, 116 164 98 8
  combo 1729, 218 162 84 80, drop
  combo 1730, 306 162 84 60, drop
  button "Remove", 1731, 400 161 56 13
  text "", 1732, 116 177 98 8
  combo 1733, 218 175 84 80, drop
  combo 1734, 306 175 84 60, drop
  button "Remove", 1735, 400 174 56 13
  text "", 1736, 116 190 98 8
  combo 1737, 218 188 84 80, drop
  combo 1738, 306 188 84 60, drop
  button "Remove", 1739, 400 187 56 13
  text "", 1740, 116 203 98 8
  combo 1741, 218 201 84 80, drop
  combo 1742, 306 201 84 60, drop
  button "Remove", 1743, 400 200 56 13
  text "", 1744, 116 216 98 8
  combo 1745, 218 214 84 80, drop
  combo 1746, 306 214 84 60, drop
  button "Remove", 1747, 400 213 56 13
  text "", 1748, 116 229 98 8
  combo 1749, 218 227 84 80, drop
  combo 1750, 306 227 84 60, drop
  button "Remove", 1751, 400 226 56 13
  text "", 1752, 116 242 98 8
  combo 1753, 218 240 84 80, drop
  combo 1754, 306 240 84 60, drop
  button "Remove", 1755, 400 239 56 13
  text "", 1756, 116 255 98 8
  combo 1757, 218 253 84 80, drop
  combo 1758, 306 253 84 60, drop
  button "Remove", 1759, 400 252 56 13
  scroll "", 1774, 463 71 11 194, range 0 0 vertical
  edit "", 1770, 116 270 98 11, autohs
  combo 1771, 218 270 84 80, drop
  combo 1772, 306 270 84 60, drop
  button "Add", 1773, 400 269 56 13
  button "Refresh", 1567, 110 294 46 13
  text "", 1568, 162 296 200 8
  box "Library", 1125, 110 38 181 83
  text "List base name", 2120, 116 48 169 8
  edit "", 2121, 116 57 169 11, autohs
  text "List delivery format", 2124, 116 71 169 8
  combo 2125, 116 80 120 80, drop
  text "File types to leave out of the list", 2128, 116 94 169 8
  edit "", 2129, 116 103 169 11, autohs
  box "Film && series", 1126, 110 125 181 79
  check "Separate film and series list", 2133, 116 135 169 10
  text "File types that go in the film list", 2136, 116 146 169 8
  edit "", 2137, 116 155 169 11, autohs
  text "File types that follow a film into its list (subtitles, .nfo, .sfv)", 2140, 116 169 169 16
  edit "", 2141, 116 186 169 11, autohs
  box "!rar packing", 1127, 299 38 181 133
  check "Enable !rar folder packing", 2145, 305 48 169 10
  text "File types a folder needs to be !rar-packable", 2148, 305 59 169 16
  edit "", 2149, 305 76 169 11, autohs
  text "RAR binary path", 2152, 305 90 169 8
  edit "", 2153, 305 99 153 11, autohs
  button "...", 2154, 460 98 14 12
  text "Largest folder !rar will pack (0 = no limit) (MB)", 2156, 305 113 169 16
  edit "", 2157, 305 130 169 11, autohs
  text "RAR pack timeout (seconds)", 2160, 305 144 169 8
  edit "", 2161, 305 153 169 11, autohs
  box "List banner", 1128, 299 175 181 71
  text "List banner file", 2164, 305 185 169 8
  edit "", 2165, 305 194 153 11, autohs
  button "...", 2166, 460 193 14 12
  text "List banner size limit (KB)", 2168, 305 208 169 8
  edit "", 2169, 305 217 169 11, autohs
  check "Length and quality in the list", 2173, 305 231 169 10
  box "Automatic rebuild", 1129, 110 38 370 83
  text "Rebuild the list automatically", 2176, 116 48 358 8
  edit "", 2177, 116 57 358 11, autohs
  text "List rebuild hard cap (seconds, 0 = none)", 2180, 116 71 358 8
  edit "", 2181, 116 80 358 11, autohs
  text "Give up if a rebuild reports nothing for (seconds)", 2184, 116 94 358 8
  edit "", 2185, 116 103 358 11, autohs
  box "Performance", 1130, 110 125 370 83
  text "Time limit for reading audio files (no longer used)", 2188, 116 135 358 8
  edit "", 2189, 116 144 358 11, autohs
  text "Audio files read at once", 2192, 116 158 358 8
  edit "", 2193, 116 167 358 11, autohs
  text "Folders scanned at once", 2196, 116 181 358 8
  edit "", 2197, 116 190 358 11, autohs
  box "Speed limits", 1131, 110 38 181 87
  text "Upload is your sends to people; download is everything the bot fetches. Each is one limit shared by all transfers that way.", 1132, 116 48 169 24
  text "Max upload speed (KB/s, 0 = no limit)", 2200, 116 75 169 8
  edit "", 2201, 116 84 169 11, autohs
  text "Max download speed (KB/s, 0 = no limit)", 2204, 116 98 169 8
  edit "", 2205, 116 107 169 11, autohs
  box "Buffers && packet size", 1133, 110 129 181 68
  text "Packet size", 2208, 116 139 169 8
  combo 2209, 116 148 120 80, drop
  text "Socket send buffer (0 = the default for your platform) (KB)", 2212, 116 162 169 16
  edit "", 2213, 116 179 169 11, autohs
  box "Ports && reliability", 1134, 299 38 181 145
  text "DCC port range start", 2216, 305 48 169 8
  edit "", 2217, 305 57 169 11, autohs
  text "DCC port range end", 2220, 305 71 169 8
  edit "", 2221, 305 80 169 11, autohs
  text "Wait for the receiver to connect (seconds)", 2224, 305 94 169 16
  edit "", 2225, 305 111 169 11, autohs
  text "Max send failures", 2228, 305 125 169 8
  edit "", 2229, 305 134 169 11, autohs
  text "Failed sends in a day before a nick is paused (0 = off)", 2232, 305 148 169 16
  edit "", 2233, 305 165 169 11, autohs
  box "Sending", 1135, 110 38 181 129
  text "Max simultaneous sends", 2236, 116 48 169 8
  edit "", 2237, 116 57 169 11, autohs
  text "One large file from (MB, 0 = off)", 2240, 116 71 169 8
  edit "", 2241, 116 80 169 11, autohs
  text "Stop sends slower than (KB/s, 0 = off)", 2244, 116 94 169 8
  edit "", 2245, 116 103 169 11, autohs
  text "Max queue per user", 2248, 116 117 169 8
  edit "", 2249, 116 126 169 11, autohs
  text "Max global queue", 2252, 116 140 169 8
  edit "", 2253, 116 149 169 11, autohs
  box "Search (@find)", 1136, 299 38 181 98
  text "How much the bot says back once it answers (the on/off switch is under General > General Settings).", 1137, 305 48 169 24
  text "Max search results", 2256, 305 75 169 8
  edit "", 2257, 305 84 169 11, autohs
  check "Name the folder in search replies", 2261, 305 98 169 10
  text "Longest folder shown (characters)", 2264, 305 109 169 8
  edit "", 2265, 305 118 169 11, autohs
  box "During a rebuild", 1138, 299 140 181 67
  check "Pause sharing during !update", 2269, 305 150 169 10
  check "Pause for the whole rebuild", 2273, 305 161 169 10
  text "Seconds a rehash waits for transfers to finish", 2276, 305 172 169 16
  edit "", 2277, 305 189 169 11, autohs
  box "Auto-grab", 1139, 110 38 370 94
  check "Grab the lists of bots you have no list from", 2281, 116 48 358 10
  text "Minutes between automatic grabs", 2284, 116 59 358 8
  edit "", 2285, 116 68 358 11, autohs
  text "Skip bots with fewer files than", 2288, 116 82 358 8
  edit "", 2289, 116 91 358 11, autohs
  text "Skip bots slower than (KB/s)", 2292, 116 105 358 8
  edit "", 2293, 116 114 358 11, autohs
  box "Discover lists", 1140, 110 136 370 48
  check "Discover a bot's other channel-bound lists", 2297, 116 146 358 10
  text "Hold stable this long first (seconds)", 2300, 116 157 358 8
  edit "", 2301, 116 166 358 11, autohs
  box "Slots && re-fetch", 1141, 110 38 181 102
  text "Max fetch slots", 2304, 116 48 169 8
  edit "", 2305, 116 57 169 11, autohs
  check "Re-fetch when a bot has a new list", 2309, 116 71 169 10
  text "Least time between re-fetches of one bot (hours)", 2312, 116 82 169 16
  edit "", 2313, 116 99 169 11, autohs
  text "Most lists to re-fetch in one sweep", 2316, 116 113 169 8
  edit "", 2317, 116 122 169 11, autohs
  box "Timeouts", 1142, 110 144 181 161
  text "Wait for a reply to a fetch request (seconds)", 2320, 116 154 169 16
  edit "", 2321, 116 171 169 11, autohs
  text "Fetch transfer timeout (seconds)", 2324, 116 185 169 8
  edit "", 2325, 116 194 169 11, autohs
  text "Wait for a reply to a folder (.rar) request (seconds)", 2328, 116 208 169 16
  edit "", 2329, 116 225 169 11, autohs
  text "...from a bot that publishes no .rar list (seconds)", 2332, 116 239 169 16
  edit "", 2333, 116 256 169 11, autohs
  text "Folder (.rar) fetch transfer timeout (seconds)", 2336, 116 270 169 16
  edit "", 2337, 116 287 169 11, autohs
  box "Size limits", 1143, 299 38 181 114
  text "Max fetch file size (MB)", 2340, 305 48 169 8
  edit "", 2341, 305 57 169 11, autohs
  text "Max folder (.rar) fetch size (MB)", 2344, 305 71 169 8
  edit "", 2345, 305 80 169 11, autohs
  text "Max fetched master-list zip size (MB)", 2348, 305 94 169 8
  edit "", 2349, 305 103 169 11, autohs
  text "Largest list text accepted from a peer (MB)", 2352, 305 117 169 16
  edit "", 2353, 305 134 169 11, autohs
  box "History", 1144, 299 156 181 60
  text "Keep finished downloads for (days)", 2356, 305 166 169 8
  edit "", 2357, 305 175 169 11, autohs
  text "Maximum finished downloads kept", 2360, 305 189 169 8
  edit "", 2361, 305 198 169 11, autohs
  box "Fetch queue", 1145, 110 38 370 106
  text "Files asked of one bot at once", 2364, 116 48 358 8
  edit "", 2365, 116 57 358 11, autohs
  text "Wait for a queued request (s)", 2368, 116 71 358 8
  edit "", 2369, 116 80 358 11, autohs
  text "Failed requests in a row that pause a bot", 2372, 116 94 358 8
  edit "", 2373, 116 103 358 11, autohs
  text "Minutes a failing bot stays paused", 2376, 116 117 358 8
  edit "", 2377, 116 126 358 11, autohs
  box "List requests", 1146, 110 148 370 37
  text "Tag on your list requests", 2380, 116 158 358 8
  edit "", 2381, 116 167 358 11, autohs
  box "Flood protection", 1147, 110 38 370 106
  text "Max commands per window", 2384, 116 48 358 8
  edit "", 2385, 116 57 358 11, autohs
  text "Request window (seconds)", 2388, 116 71 358 8
  edit "", 2389, 116 80 358 11, autohs
  text "Mute duration (seconds)", 2392, 116 94 358 8
  edit "", 2393, 116 103 358 11, autohs
  text "Ban after flooding while muted (seconds)", 2396, 116 117 358 8
  edit "", 2397, 116 126 358 11, autohs
  box "Temporary (by nick)", 1148, 110 38 370 121
  text "Flood protection bans a nick by itself; Ignore is the same timed block, started by hand.", 1149, 116 48 358 8
  list 1580, 116 59 358 60, vsbar
  text "Nick", 1583, 116 125 20 8
  edit "", 1584, 138 123 90 11, autohs
  text "Minutes", 1585, 234 125 32 8
  edit "", 1586, 268 123 30 11, autohs
  button "Ignore", 1587, 304 122 50 13
  button "Lift now", 1581, 116 138 50 13
  button "Refresh", 1582, 169 138 46 13
  box "Permanent (by hostmask pattern)", 1150, 110 163 370 129
  text "Wildcard patterns such as *!*@host.example - the same as ban and unban in the console.", 1151, 116 173 358 16
  list 1595, 116 192 358 60, vsbar
  edit "", 1597, 116 256 250 11, autohs
  button "Add pattern", 1598, 370 255 56 13
  button "Remove", 1596, 116 271 50 13
  box "Auto-reply", 1152, 110 38 370 83
  text "Record one private message per sender every (seconds)", 2400, 116 48 358 8
  edit "", 2401, 116 57 358 11, autohs
  text "", 2404, 116 71 358 8
  edit "", 2405, 116 80 358 11, autohs
  text "Reply to the same sender once every (seconds)", 2408, 116 94 358 8
  edit "", 2409, 116 103 358 11, autohs
  box "Rate limit", 1153, 110 125 370 60
  text "Most replies to send in one burst window", 2412, 116 135 358 8
  edit "", 2413, 116 144 358 11, autohs
  text "How long that burst window is (seconds)", 2416, 116 158 358 8
  edit "", 2417, 116 167 358 11, autohs
  box "Access", 1154, 110 38 370 60
  text "Admin hostmasks", 2420, 116 48 358 8
  edit "", 2421, 116 57 358 11, autohs
  text "DCC chat connection mode", 2424, 116 71 358 8
  combo 2425, 116 80 120 80, drop
  box "Behaviour", 1155, 110 102 370 36
  check "Allow admin commands in channel", 2429, 116 112 358 10
  check "Colour the tags in the admin DCC chat", 2433, 116 123 358 10
  box "Server", 1156, 110 38 370 83
  text "Host", 2436, 116 48 358 8
  edit "", 2437, 116 57 358 11, autohs
  text "Port", 2440, 116 71 358 8
  edit "", 2441, 116 80 358 11, autohs
  text "Other names the dashboard answers to", 2444, 116 94 358 8
  edit "", 2445, 116 103 358 11, autohs
  box "Console && browser", 1157, 110 125 370 59
  text "Enable the Console page (remote admin)", 2448, 116 135 358 8
  combo 2449, 116 144 120 80, drop
  check "Open the dashboard in a browser at startup", 2453, 116 158 358 10
  check "Folder picker on the Settings page", 2457, 116 169 358 10
  box "What shows in @DCCore", 1158, 110 38 370 69
  check "Show requests (who asked for what)", 2461, 116 48 358 10
  check "Show queue positions", 2465, 116 59 358 10
  check "Show transfers starting, resuming and completing", 2469, 116 70 358 10
  check "Show failed transfers", 2473, 116 81 358 10
  check "Show searches and their result counts", 2477, 116 92 358 10
  box "Debug channel", 1159, 110 111 370 25
  check "Also send requests, queue positions, starts and searches to the IRC debug channel", 2481, 116 121 358 10
  box "Debug", 1160, 110 38 370 47
  check "Debug mode", 2485, 116 48 358 10
  check "Send debug lines to channel", 2489, 116 59 358 10
  check "Send debug lines to admin console", 2493, 116 70 358 10
  box "Logging", 1161, 110 89 370 106
  text "Time prefix on every console line (strftime; blank = none)", 2496, 116 99 358 8
  edit "", 2497, 116 108 358 11, autohs
  text "Log file (blank = none)", 2500, 116 122 358 8
  edit "", 2501, 116 131 342 11, autohs
  button "...", 2502, 460 130 14 12
  text "Start a new log file at (MB)", 2504, 116 145 358 8
  edit "", 2505, 116 154 358 11, autohs
  text "Old log files to keep", 2508, 116 168 358 8
  edit "", 2509, 116 177 358 11, autohs
  box "Window && project", 1162, 110 199 370 60
  text "The bot's window on Windows (normal, minimised, hidden)", 2512, 116 209 358 8
  combo 2513, 116 218 120 80, drop
  text "Project URL", 2516, 116 232 358 8
  edit "", 2517, 116 241 358 11, autohs
  box "Library && lists", 1163, 110 38 181 90
  text "Temp archive directory", 2520, 116 50 81 16
  edit "", 2521, 201 48 68 11, autohs
  button "...", 2522, 271 47 14 12
  text "Master list directory", 2524, 116 70 81 8
  edit "", 2525, 201 68 68 11, autohs
  button "...", 2526, 271 67 14 12
  text "Fetched files directory", 2528, 116 82 81 16
  edit "", 2529, 201 80 68 11, autohs
  button "...", 2530, 271 79 14 12
  text "Served folders file", 2532, 116 102 81 8
  edit "", 2533, 201 100 68 11, autohs
  button "...", 2534, 271 99 14 12
  text "Served lists file", 2536, 116 114 81 8
  edit "", 2537, 201 112 68 11, autohs
  button "...", 2538, 271 111 14 12
  box "Security", 1164, 110 132 181 38
  text "Bans file", 2540, 116 144 81 8
  edit "", 2541, 201 142 68 11, autohs
  button "...", 2542, 271 141 14 12
  text "Hard bans file", 2544, 116 156 81 8
  edit "", 2545, 201 154 68 11, autohs
  button "...", 2546, 271 153 14 12
  box "Stats && bots", 1165, 110 174 181 50
  text "Stats file", 2548, 116 186 81 8
  edit "", 2549, 201 184 68 11, autohs
  button "...", 2550, 271 183 14 12
  text "Known bots file", 2552, 116 198 81 8
  edit "", 2553, 201 196 68 11, autohs
  button "...", 2554, 271 195 14 12
  text "Fetched bot lists file", 2556, 116 210 81 8
  edit "", 2557, 201 208 68 11, autohs
  button "...", 2558, 271 207 14 12
  box "Search && audio", 1166, 110 228 181 46
  text "Cross-list search index", 2560, 116 240 81 16
  edit "", 2561, 201 238 68 11, autohs
  button "...", 2562, 271 237 14 12
  text "Audio info cache", 2564, 116 260 81 8
  edit "", 2565, 201 258 68 11, autohs
  button "...", 2566, 271 257 14 12
  box "Fetch history", 1167, 299 38 181 58
  text "Fetch history file", 2568, 305 50 81 8
  edit "", 2569, 390 48 68 11, autohs
  button "...", 2570, 460 47 14 12
  text "Download counts file", 2572, 305 62 81 16
  edit "", 2573, 390 60 68 11, autohs
  button "...", 2574, 460 59 14 12
  text "Transfer record file", 2576, 305 82 81 8
  edit "", 2577, 390 80 68 11, autohs
  button "...", 2578, 460 79 14 12
  box "List build", 1168, 299 100 181 58
  text "List size file", 2580, 305 112 81 8
  edit "", 2581, 390 110 68 11, autohs
  button "...", 2582, 460 109 14 12
  text "List raw bytes file", 2584, 305 124 81 8
  edit "", 2585, 390 122 68 11, autohs
  button "...", 2586, 460 121 14 12
  text "List rebuild progress file", 2588, 305 136 81 16
  edit "", 2589, 390 134 68 11, autohs
  button "...", 2590, 460 133 14 12
  box "Console && messaging", 1169, 299 162 181 106
  text "Paired console scripts file", 2592, 305 174 81 16
  edit "", 2593, 390 172 68 11, autohs
  button "...", 2594, 460 171 14 12
  text "On-connect commands file", 2596, 305 194 81 16
  edit "", 2597, 390 192 68 11, autohs
  button "...", 2598, 460 191 14 12
  text "Operator notices file", 2600, 305 214 81 16
  edit "", 2601, 390 212 68 11, autohs
  button "...", 2602, 460 211 14 12
  text "Private messages file", 2604, 305 234 81 16
  edit "", 2605, 390 232 68 11, autohs
  button "...", 2606, 460 231 14 12
  text "DCC queue file", 2608, 305 254 81 8
  edit "", 2609, 390 252 68 11, autohs
  button "...", 2610, 460 251 14 12
  box "Show in @DCCore", 1170, 110 38 370 133
  check "Requests (who asked for what)", 1620, 116 49 176 10
  combo 1621, 304 48 96 80, drop
  check "Queue positions", 1622, 116 62 176 10
  combo 1623, 304 61 96 80, drop
  check "Sends: starting, resuming, done", 1624, 116 75 176 10
  combo 1625, 304 74 96 80, drop
  check "Failed transfers", 1626, 116 88 176 10
  combo 1627, 304 87 96 80, drop
  check "Searches and result counts", 1628, 116 101 176 10
  combo 1629, 304 100 96 80, drop
  check "Joins, parts, quits of queued users", 1630, 116 114 176 10
  combo 1631, 304 113 96 80, drop
  check "Bans, mutes, floods", 1632, 116 127 176 10
  combo 1633, 304 126 96 80, drop
  check "Other log lines", 1634, 116 140 176 10
  combo 1635, 304 139 96 80, drop
  text "Status every", 1636, 116 154 60 8
  edit "", 1637, 178 152 24 11, autohs
  text "min, 0 = off", 1638, 210 154 70 8
  box "Colours", 1171, 110 175 370 122
  text "Search text is the term searched for. Each of the three can be the same as its line; Search text can be the same as File names, and Nicks can be per nick, a colour of its own from the name.", 1172, 116 185 358 24
  text "File names", 1660, 116 214 110 8
  combo 1640, 230 212 96 80, drop
  text "Console replies", 1661, 116 227 110 8
  combo 1641, 230 225 96 80, drop
  text "Panel headings", 1662, 116 240 110 8
  combo 1642, 230 238 96 80, drop
  text "Search text", 1663, 116 253 110 8
  combo 1643, 230 251 96 80, drop
  text "Nicks", 1664, 116 266 110 8
  combo 1644, 230 264 96 80, drop
  text "Channels", 1665, 116 279 110 8
  combo 1645, 230 277 96 80, drop
  box "Window", 1173, 110 38 370 99
  check "Side panel: slots, queue and totals", 2613, 116 48 358 10
  check "Slots, queue and speed in the title bar", 2617, 116 59 358 10
  check "Console replies in a separate window", 2621, 116 70 358 10
  check "Beep on a failed transfer", 2625, 116 81 358 10
  check "Fixed-width font, size", 1670, 116 93 110 10
  edit "", 1671, 250 92 24 11, autohs
  text "Background", 1672, 116 107 120 8
  combo 1673, 250 105 96 80, drop
  text "Finished downloads to show", 1674, 116 120 120 8
  edit "", 1675, 250 118 24 11, autohs
  box "Theme", 1174, 110 141 370 78
  text "Dark colours for the script's windows - the control panel, the list window, the download queues - and an icon beside each of the script's menu items and pages. Follow mIRC takes mIRC's own dark mode (7.79 and later). The control panel takes a change the next time it opens; /dccore theme says what applies and why.", 1175, 116 151 358 32
  text "Theme", 1690, 116 188 120 8
  combo 1691, 250 186 96 80, drop
  check "Icons in the menus and the control panel's page list", 1692, 116 201 358 10
  box "Settings window", 1176, 110 223 370 52
  text "Drawn with DCX (scripts\mirc\dcx) when this mIRC can load it, with mIRC's own controls when not - /dccore dcx says which and why. Takes effect the next time the window opens.", 1177, 116 233 358 24
  check "Use the built-in settings window, not DCX", 2629, 116 260 358 10
  box "Bot", 1178, 110 38 370 78
  text "The bot this mIRC is paired with. Reconnecting and logging in by itself when it comes back is under General > General Settings.", 1179, 116 48 358 16
  text "Bot nick", 1680, 116 69 36 8
  edit "", 1681, 156 67 80 11, autohs
  text "", 1682, 242 69 232 8
  button "Pair again...", 1683, 116 81 56 13
  button "Forget token", 1684, 175 81 56 13
  check "Console feed on (also requests and sends - not just status)", 1685, 116 98 358 10
  box "DCCore Chat (public)", 1180, 110 38 370 36
  check "Listen on every channel the bot is in, not only the ticked ones", 2633, 116 48 358 10
  check "Open the chat window when a line arrives", 2637, 116 59 358 10
  box "History", 1181, 110 78 370 79
  text "Whole months of the history file are deleted: those that ended longer ago than the days, then the oldest while the folder is over the size. 0 is no limit.", 1182, 116 88 358 16
  text "Keep for (days)", 2640, 116 107 358 8
  edit "", 2641, 116 116 358 11, autohs
  text "At most (MB)", 2644, 116 130 358 8
  edit "", 2645, 116 139 358 11, autohs
}
alias dccore.sw.data {
  if ($hget(dccore.swm)) { hfree dccore.swm }
  hmake dccore.swm 100
  hadd dccore.swm groups 7
  hadd dccore.swm slots 6
  hadd dccore.swm size 488 364
  hadd dccore.swm dcxpages 900 10 24 88 286
  hadd dccore.swm dcxtitle 901 110 26 370 10
  hadd dccore.swm dcxtabs 800 3 2 14 2 6
  hadd dccore.swm dcxbody 830 4 16 480 324
  hadd dccore.swm dcxcorner.o 0,7 0,4 1,4 1,3 2,3 2,2 3,2 3,1 4,1 4,0
  hadd dccore.swm dcxcorner.i 1,7 1,4 2,4 2,3 3,3 3,2 4,2 4,1
  hadd dccore.swm dcxback 840 8 3 14 12
  hadd dccore.swm dcxcrumb 843 26 5 22 9
  hadd dccore.swm home.n 2
  hadd dccore.swm home.1 844 98 28 140 24
  hadd dccore.swm home.2 848 250 28 140 24
  hadd dccore.swm lvs 1520 1540 1580 1595
  hadd dccore.swm g.1 General
  hadd dccore.swm g.1.pages 1 2 3 4 5 6
  hadd dccore.swm g.2 Sharing
  hadd dccore.swm g.2.pages 7 8 9 10 11 12
  hadd dccore.swm g.3 Downloads
  hadd dccore.swm g.3.pages 13 14 15
  hadd dccore.swm g.4 Security
  hadd dccore.swm g.4.pages 16 17 18 19
  hadd dccore.swm g.5 Dashboard & Console
  hadd dccore.swm g.5.pages 20 21
  hadd dccore.swm g.6 Advanced
  hadd dccore.swm g.6.pages 22 23
  hadd dccore.swm g.7 mIRC
  hadd dccore.swm g.7.pages 24 25 26 27
  hadd dccore.swm home.1.t Bot
  hadd dccore.swm home.1.s The bot's settings, sent with Apply
  hadd dccore.swm home.2.t mIRC
  hadd dccore.swm home.2.s This mIRC's own: window, link, chat
  hadd dccore.swm home.f.n 4
  hadd dccore.swm home.f.ids 300-345
  hadd dccore.swm home.f.1 300 301 12 66 228 46 18 62 15 9 kv Bot
  hadd dccore.swm home.f.1.n 4
  hadd dccore.swm home.f.1.r.1 302 303 18 72 52 8 74 72 160 8 Nick
  hadd dccore.swm home.f.1.r.2 304 305 18 81 52 8 74 81 160 8 Network
  hadd dccore.swm home.f.1.r.3 306 307 18 90 52 8 74 90 160 8 Up since
  hadd dccore.swm home.f.1.r.4 308 309 18 99 52 8 74 99 160 8 Connection
  hadd dccore.swm home.f.2 310 311 248 66 228 64 254 62 50 9 full Sending
  hadd dccore.swm home.f.2.n 6
  hadd dccore.swm home.f.2.r.1 312 0 254 72 216 8 0 0 0 0
  hadd dccore.swm home.f.2.r.2 313 0 254 81 216 8 0 0 0 0
  hadd dccore.swm home.f.2.r.3 314 0 254 90 216 8 0 0 0 0
  hadd dccore.swm home.f.2.r.4 315 0 254 99 216 8 0 0 0 0
  hadd dccore.swm home.f.2.r.5 316 0 254 108 216 8 0 0 0 0
  hadd dccore.swm home.f.2.r.6 317 0 254 117 216 8 0 0 0 0
  hadd dccore.swm home.f.3 318 319 12 122 228 55 18 118 24 9 kv Today
  hadd dccore.swm home.f.3.n 5
  hadd dccore.swm home.f.3.r.1 320 321 18 128 52 8 74 128 160 8 Sent
  hadd dccore.swm home.f.3.r.2 322 323 18 137 52 8 74 137 160 8 Queue
  hadd dccore.swm home.f.3.r.3 324 325 18 146 52 8 74 146 160 8 Speed record
  hadd dccore.swm home.f.3.r.4 326 327 18 155 52 8 74 155 160 8 Failed
  hadd dccore.swm home.f.3.r.5 328 329 18 164 52 8 74 164 160 8 Searches
  hadd dccore.swm home.f.4 330 331 248 140 228 73 254 136 26 9 kv Library
  hadd dccore.swm home.f.4.n 7
  hadd dccore.swm home.f.4.r.1 332 333 254 146 52 8 310 146 160 8 Lists
  hadd dccore.swm home.f.4.r.2 334 335 254 155 52 8 310 155 160 8 Files
  hadd dccore.swm home.f.4.r.3 336 337 254 164 52 8 310 164 160 8 Size
  hadd dccore.swm home.f.4.r.4 338 339 254 173 52 8 310 173 160 8 Last rebuild
  hadd dccore.swm home.f.4.r.5 340 341 254 182 52 8 310 182 160 8 Search index
  hadd dccore.swm home.f.4.r.6 342 343 254 191 52 8 310 191 160 8 Rebuild
  hadd dccore.swm home.f.4.r.7 344 345 254 200 52 8 310 200 160 8 Packing
  hadd dccore.swm tab.1 50 35
  hadd dccore.swm tab.2 86 34
  hadd dccore.swm tab.3 121 45
  hadd dccore.swm tab.4 167 36
  hadd dccore.swm tab.5 204 79
  hadd dccore.swm tab.6 284 42
  hadd dccore.swm fr.1100 100 110 42 370 102 116 38 40 9 Connection
  hadd dccore.swm fr.1101 102 110 152 370 56 116 148 48 9 Reconnection
  hadd dccore.swm fr.1102 104 110 216 370 87 116 212 41 9 On connect
  hadd dccore.swm fr.1104 106 110 57 370 43 116 53 118 9 Open when mIRC starts (minimised)
  hadd dccore.swm fr.1105 108 110 108 370 21 116 104 40 9 Connection
  hadd dccore.swm fr.1106 110 110 137 370 32 116 133 58 9 Sharing & search
  hadd dccore.swm fr.1107 112 110 177 370 43 116 173 80 9 Messaging & dashboard
  hadd dccore.swm fr.1108 114 110 228 370 21 116 224 31 9 Updates
  hadd dccore.swm fr.1109 116 110 42 370 52 116 38 54 9 Admin channels
  hadd dccore.swm fr.1111 118 110 102 370 209 116 98 58 9 Serving channels
  hadd dccore.swm fr.1113 120 110 42 370 33 116 38 24 9 Admin
  hadd dccore.swm fr.1114 122 110 42 370 53 116 38 78 9 What the channel sees
  hadd dccore.swm fr.1115 124 110 103 370 98 116 99 112 9 Custom override - surface colours
  hadd dccore.swm fr.1117 126 110 209 370 79 116 205 102 9 Custom override - text colours
  hadd dccore.swm fr.1118 128 110 42 370 33 116 38 26 9 Advert
  hadd dccore.swm fr.1119 130 110 83 370 56 116 79 60 9 Broadcast search
  hadd dccore.swm fr.1120 132 110 147 370 56 116 143 24 9 Timing
  hadd dccore.swm fr.1122 134 110 73 370 95 116 69 18 9 Lists
  hadd dccore.swm fr.1124 136 110 65 370 225 116 61 58 9 Channel bindings
  hadd dccore.swm fr.1125 138 110 42 181 79 116 38 26 9 Library
  hadd dccore.swm fr.1126 140 110 129 181 75 116 125 44 9 Film & series
  hadd dccore.swm fr.1127 142 299 42 181 129 305 38 42 9 !rar packing
  hadd dccore.swm fr.1128 144 299 179 181 67 305 175 40 9 List banner
  hadd dccore.swm fr.1129 146 110 42 370 79 116 38 60 9 Automatic rebuild
  hadd dccore.swm fr.1130 148 110 129 370 79 116 125 45 9 Performance
  hadd dccore.swm fr.1131 150 110 42 181 83 116 38 42 9 Speed limits
  hadd dccore.swm fr.1133 152 110 133 181 64 116 129 72 9 Buffers & packet size
  hadd dccore.swm fr.1134 154 299 42 181 141 305 38 58 9 Ports & reliability
  hadd dccore.swm fr.1135 156 110 42 181 125 116 38 30 9 Sending
  hadd dccore.swm fr.1136 158 299 42 181 94 305 38 52 9 Search (@find)
  hadd dccore.swm fr.1138 160 299 144 181 63 305 140 54 9 During a rebuild
  hadd dccore.swm fr.1139 162 110 42 370 90 116 38 37 9 Auto-grab
  hadd dccore.swm fr.1140 164 110 140 370 44 116 136 46 9 Discover lists
  hadd dccore.swm fr.1141 166 110 42 181 98 116 38 54 9 Slots & re-fetch
  hadd dccore.swm fr.1142 168 110 148 181 157 116 144 33 9 Timeouts
  hadd dccore.swm fr.1143 170 299 42 181 110 305 38 34 9 Size limits
  hadd dccore.swm fr.1144 172 299 160 181 56 305 156 27 9 History
  hadd dccore.swm fr.1145 174 110 42 370 102 116 38 44 9 Fetch queue
  hadd dccore.swm fr.1146 176 110 152 370 33 116 148 45 9 List requests
  hadd dccore.swm fr.1147 178 110 42 370 102 116 38 56 9 Flood protection
  hadd dccore.swm fr.1148 180 110 42 370 117 116 38 68 9 Temporary (by nick)
  hadd dccore.swm fr.1150 182 110 167 370 125 116 163 112 9 Permanent (by hostmask pattern)
  hadd dccore.swm fr.1152 184 110 42 370 79 116 38 38 9 Auto-reply
  hadd dccore.swm fr.1153 186 110 129 370 56 116 125 34 9 Rate limit
  hadd dccore.swm fr.1154 188 110 42 370 56 116 38 26 9 Access
  hadd dccore.swm fr.1155 190 110 106 370 32 116 102 36 9 Behaviour
  hadd dccore.swm fr.1156 192 110 42 370 79 116 38 26 9 Server
  hadd dccore.swm fr.1157 194 110 129 370 55 116 125 64 9 Console & browser
  hadd dccore.swm fr.1158 196 110 42 370 65 116 38 84 9 What shows in @DCCore
  hadd dccore.swm fr.1159 198 110 115 370 21 116 111 52 9 Debug channel
  hadd dccore.swm fr.1160 200 110 42 370 43 116 38 25 9 Debug
  hadd dccore.swm fr.1161 202 110 93 370 102 116 89 29 9 Logging
  hadd dccore.swm fr.1162 204 110 203 370 56 116 199 61 9 Window & project
  hadd dccore.swm fr.1163 206 110 42 181 86 116 38 47 9 Library & lists
  hadd dccore.swm fr.1164 208 110 136 181 34 116 132 30 9 Security
  hadd dccore.swm fr.1165 210 110 178 181 46 116 174 44 9 Stats & bots
  hadd dccore.swm fr.1166 212 110 232 181 42 116 228 52 9 Search & audio
  hadd dccore.swm fr.1167 214 299 42 181 54 305 38 46 9 Fetch history
  hadd dccore.swm fr.1168 216 299 104 181 54 305 100 32 9 List build
  hadd dccore.swm fr.1169 218 299 166 181 102 305 162 72 9 Console & messaging
  hadd dccore.swm fr.1170 220 110 42 370 129 116 38 62 9 Show in @DCCore
  hadd dccore.swm fr.1171 222 110 179 370 118 116 175 28 9 Colours
  hadd dccore.swm fr.1173 224 110 42 370 95 116 38 30 9 Window
  hadd dccore.swm fr.1174 226 110 145 370 74 116 141 26 9 Theme
  hadd dccore.swm fr.1176 228 110 227 370 48 116 223 56 9 Settings window
  hadd dccore.swm fr.1178 230 110 42 370 74 116 38 15 9 Bot
  hadd dccore.swm fr.1180 232 110 42 370 32 116 38 72 9 DCCore Chat (public)
  hadd dccore.swm fr.1181 234 110 82 370 75 116 78 27 9 History
  hadd dccore.swm lv.1520 910 116 135 250 168
  hadd dccore.swm lvc.1520 0 Channel
  hadd dccore.swm lvx.1520 noheader
  hadd dccore.swm lv.1540 911 116 79 358 64
  hadd dccore.swm lvc.1540 120 Name,44 Primary,0 Folders
  hadd dccore.swm lv.1580 913 116 59 358 60
  hadd dccore.swm lvc.1580 160 Nick,0 Time left
  hadd dccore.swm lv.1595 914 116 192 358 60
  hadd dccore.swm lvc.1595 0 Pattern
  hadd dccore.swm pages 27
  hadd dccore.swm p.1 IRC Server
  hadd dccore.swm p.1.layout roomy 1
  hadd dccore.swm p.1.n 1
  hadd dccore.swm p.1.1 1100,2000,2001,2004,2005,2008,2009,2012,2013,1101,2016,2017,2020,2021,1102,1500,1501,1502,1503,1506,1505
  hadd dccore.swm p.1.bx 1100,1101,1102
  hadd dccore.swm p.1.fr 100-105
  hadd dccore.swm p.1.w onconnect
  hadd dccore.swm p.1.ask onconnect
  hadd dccore.swm p.2 General Settings
  hadd dccore.swm p.2.layout roomy 1
  hadd dccore.swm p.2.n 1
  hadd dccore.swm p.2.1 1103,1104,2025,2029,2033,1105,2037,1106,2041,2045,1107,2049,2053,2057,1108,2061
  hadd dccore.swm p.2.bx 1104,1105,1106,1107,1108
  hadd dccore.swm p.2.fr 106-115
  hadd dccore.swm p.3 Channels
  hadd dccore.swm p.3.layout roomy 1
  hadd dccore.swm p.3.n 1
  hadd dccore.swm p.3.1 1109,1110,2064,2065,1111,1112,1520,1521,1522,1523
  hadd dccore.swm p.3.lv 1520
  hadd dccore.swm p.3.bx 1109,1111
  hadd dccore.swm p.3.fr 116-119
  hadd dccore.swm p.3.w channels
  hadd dccore.swm p.4 Operator
  hadd dccore.swm p.4.layout roomy 1
  hadd dccore.swm p.4.n 1
  hadd dccore.swm p.4.1 1113,2068,2069
  hadd dccore.swm p.4.bx 1113
  hadd dccore.swm p.4.fr 120-121
  hadd dccore.swm p.5 Appearance
  hadd dccore.swm p.5.layout roomy 1
  hadd dccore.swm p.5.n 1
  hadd dccore.swm p.5.1 1114,2072,2073,1610,1611,1115,1116,2076,2077,2078,2080,2081,2082,2084,2085,2086,1117,2088,2089,2090,2092,2093,2094,2096,2097,2098
  hadd dccore.swm p.5.bx 1114,1115,1117
  hadd dccore.swm p.5.fr 122-127
  hadd dccore.swm p.5.w preview
  hadd dccore.swm p.6 Advertising
  hadd dccore.swm p.6.layout roomy 1
  hadd dccore.swm p.6.n 1
  hadd dccore.swm p.6.1 1118,2100,2101,1119,2104,2105,2108,2109,1120,2112,2113,2116,2117
  hadd dccore.swm p.6.bx 1118,1119,1120
  hadd dccore.swm p.6.fr 128-133
  hadd dccore.swm p.7 Lists
  hadd dccore.swm p.7.layout roomy 1
  hadd dccore.swm p.7.n 1
  hadd dccore.swm p.7.1 1121,1122,1540,1551,1554,1555,1557
  hadd dccore.swm p.7.lv 1540
  hadd dccore.swm p.7.bx 1122
  hadd dccore.swm p.7.fr 134-135
  hadd dccore.swm p.7.w srvlists
  hadd dccore.swm p.7.ask served
  hadd dccore.swm p.8 Channel bindings
  hadd dccore.swm p.8.layout roomy 1
  hadd dccore.swm p.8.n 2
  hadd dccore.swm p.8.1 1123,1124,1700,1701,1702,1703,1704,1705,1706,1707,1708,1709,1710,1711,1712,1713,1714,1715,1716,1717,1718,1719,1720,1721,1722,1723,1724,1725,1726,1727,1728,1729,1730,1731,1732,1733,1734,1735,1736,1737,1738,1739,1740,1741,1742,1743,1744,1745,1746,1747
  hadd dccore.swm p.8.2 1748,1749,1750,1751,1752,1753,1754,1755,1756,1757,1758,1759,1774,1770,1771,1772,1773,1567,1568
  hadd dccore.swm p.8.bx 1124
  hadd dccore.swm p.8.fr 136-137
  hadd dccore.swm p.8.w srvchans srvsaveb
  hadd dccore.swm p.8.ask served
  hadd dccore.swm p.9 List settings
  hadd dccore.swm p.9.layout roomy 2
  hadd dccore.swm p.9.n 1
  hadd dccore.swm p.9.1 1125,2120,2121,2124,2125,2128,2129,1126,2133,2136,2137,2140,2141,1127,2145,2148,2149,2152,2153,2154,2156,2157,2160,2161,1128,2164,2165,2166,2168,2169,2173
  hadd dccore.swm p.9.bx 1125,1126,1127,1128
  hadd dccore.swm p.9.fr 138-145
  hadd dccore.swm p.10 Rebuild
  hadd dccore.swm p.10.layout roomy 1
  hadd dccore.swm p.10.n 1
  hadd dccore.swm p.10.1 1129,2176,2177,2180,2181,2184,2185,1130,2188,2189,2192,2193,2196,2197
  hadd dccore.swm p.10.bx 1129,1130
  hadd dccore.swm p.10.fr 146-149
  hadd dccore.swm p.11 Transfers
  hadd dccore.swm p.11.layout roomy 2
  hadd dccore.swm p.11.n 1
  hadd dccore.swm p.11.1 1131,1132,2200,2201,2204,2205,1133,2208,2209,2212,2213,1134,2216,2217,2220,2221,2224,2225,2228,2229,2232,2233
  hadd dccore.swm p.11.bx 1131,1133,1134
  hadd dccore.swm p.11.fr 150-155
  hadd dccore.swm p.12 Search
  hadd dccore.swm p.12.layout roomy 2
  hadd dccore.swm p.12.n 1
  hadd dccore.swm p.12.1 1135,2236,2237,2240,2241,2244,2245,2248,2249,2252,2253,1136,1137,2256,2257,2261,2264,2265,1138,2269,2273,2276,2277
  hadd dccore.swm p.12.bx 1135,1136,1138
  hadd dccore.swm p.12.fr 156-161
  hadd dccore.swm p.13 List discovery
  hadd dccore.swm p.13.layout roomy 1
  hadd dccore.swm p.13.n 1
  hadd dccore.swm p.13.1 1139,2281,2284,2285,2288,2289,2292,2293,1140,2297,2300,2301
  hadd dccore.swm p.13.bx 1139,1140
  hadd dccore.swm p.13.fr 162-165
  hadd dccore.swm p.14 Fetch tuning
  hadd dccore.swm p.14.layout roomy 2
  hadd dccore.swm p.14.n 1
  hadd dccore.swm p.14.1 1141,2304,2305,2309,2312,2313,2316,2317,1142,2320,2321,2324,2325,2328,2329,2332,2333,2336,2337,1143,2340,2341,2344,2345,2348,2349,2352,2353,1144,2356,2357,2360,2361
  hadd dccore.swm p.14.bx 1141,1142,1143,1144
  hadd dccore.swm p.14.fr 166-173
  hadd dccore.swm p.15 Queue
  hadd dccore.swm p.15.layout roomy 1
  hadd dccore.swm p.15.n 1
  hadd dccore.swm p.15.1 1145,2364,2365,2368,2369,2372,2373,2376,2377,1146,2380,2381
  hadd dccore.swm p.15.bx 1145,1146
  hadd dccore.swm p.15.fr 174-177
  hadd dccore.swm p.16 Anti-flood
  hadd dccore.swm p.16.layout roomy 1
  hadd dccore.swm p.16.n 1
  hadd dccore.swm p.16.1 1147,2384,2385,2388,2389,2392,2393,2396,2397
  hadd dccore.swm p.16.bx 1147
  hadd dccore.swm p.16.fr 178-179
  hadd dccore.swm p.17 Bans & ignores
  hadd dccore.swm p.17.layout roomy 1
  hadd dccore.swm p.17.n 1
  hadd dccore.swm p.17.1 1148,1149,1580,1583,1584,1585,1586,1587,1581,1582,1150,1151,1595,1597,1598,1596
  hadd dccore.swm p.17.lv 1580 1595
  hadd dccore.swm p.17.bx 1148,1150
  hadd dccore.swm p.17.fr 180-183
  hadd dccore.swm p.17.w bantimed banperm
  hadd dccore.swm p.17.ask banlist
  hadd dccore.swm p.18 Private messages
  hadd dccore.swm p.18.layout roomy 1
  hadd dccore.swm p.18.n 1
  hadd dccore.swm p.18.1 1152,2400,2401,2404,2405,2408,2409,1153,2412,2413,2416,2417
  hadd dccore.swm p.18.bx 1152,1153
  hadd dccore.swm p.18.fr 184-187
  hadd dccore.swm p.19 Admin console
  hadd dccore.swm p.19.layout roomy 1
  hadd dccore.swm p.19.n 1
  hadd dccore.swm p.19.1 1154,2420,2421,2424,2425,1155,2429,2433
  hadd dccore.swm p.19.bx 1154,1155
  hadd dccore.swm p.19.fr 188-191
  hadd dccore.swm p.20 Web dashboard
  hadd dccore.swm p.20.layout roomy 1
  hadd dccore.swm p.20.n 1
  hadd dccore.swm p.20.1 1156,2436,2437,2440,2441,2444,2445,1157,2448,2449,2453,2457
  hadd dccore.swm p.20.bx 1156,1157
  hadd dccore.swm p.20.fr 192-195
  hadd dccore.swm p.21 Console feed
  hadd dccore.swm p.21.layout roomy 1
  hadd dccore.swm p.21.n 1
  hadd dccore.swm p.21.1 1158,2461,2465,2469,2473,2477,1159,2481
  hadd dccore.swm p.21.bx 1158,1159
  hadd dccore.swm p.21.fr 196-199
  hadd dccore.swm p.22 Debug & logging
  hadd dccore.swm p.22.layout roomy 1
  hadd dccore.swm p.22.n 1
  hadd dccore.swm p.22.1 1160,2485,2489,2493,1161,2496,2497,2500,2501,2502,2504,2505,2508,2509,1162,2512,2513,2516,2517
  hadd dccore.swm p.22.bx 1160,1161,1162
  hadd dccore.swm p.22.fr 200-205
  hadd dccore.swm p.23 File locations
  hadd dccore.swm p.23.layout compact 2
  hadd dccore.swm p.23.n 2
  hadd dccore.swm p.23.1 1163,2520,2521,2522,2524,2525,2526,2528,2529,2530,2532,2533,2534,2536,2537,2538,1164,2540,2541,2542,2544,2545,2546,1165,2548,2549,2550,2552,2553,2554,2556,2557,2558,1166,2560,2561,2562,2564,2565,2566,1167,2568,2569,2570,2572,2573,2574,2576,2577,2578
  hadd dccore.swm p.23.2 1168,2580,2581,2582,2584,2585,2586,2588,2589,2590,1169,2592,2593,2594,2596,2597,2598,2600,2601,2602,2604,2605,2606,2608,2609,2610
  hadd dccore.swm p.23.bx 1163,1164,1165,1166,1167,1168,1169
  hadd dccore.swm p.23.fr 206-219
  hadd dccore.swm p.24 @DCCore window
  hadd dccore.swm p.24.layout roomy 1
  hadd dccore.swm p.24.n 1
  hadd dccore.swm p.24.1 1170,1620,1621,1622,1623,1624,1625,1626,1627,1628,1629,1630,1631,1632,1633,1634,1635,1636,1637,1638,1171,1172,1660,1640,1661,1641,1662,1642,1663,1643,1664,1644,1665,1645
  hadd dccore.swm p.24.bx 1170,1171
  hadd dccore.swm p.24.fr 220-223
  hadd dccore.swm p.24.w showin showcolours
  hadd dccore.swm p.25 Window
  hadd dccore.swm p.25.layout roomy 1
  hadd dccore.swm p.25.n 1
  hadd dccore.swm p.25.1 1173,2613,2617,2621,2625,1670,1671,1672,1673,1674,1675,1174,1175,1690,1691,1692,1176,1177,2629
  hadd dccore.swm p.25.bx 1173,1174,1176
  hadd dccore.swm p.25.fr 224-229
  hadd dccore.swm p.25.w winlook theme
  hadd dccore.swm p.26 Connection
  hadd dccore.swm p.26.layout roomy 1
  hadd dccore.swm p.26.n 1
  hadd dccore.swm p.26.1 1178,1179,1680,1681,1682,1683,1684,1685
  hadd dccore.swm p.26.bx 1178
  hadd dccore.swm p.26.fr 230-231
  hadd dccore.swm p.26.w botlink
  hadd dccore.swm p.27 Chat
  hadd dccore.swm p.27.layout roomy 1
  hadd dccore.swm p.27.n 1
  hadd dccore.swm p.27.1 1180,2633,2637,1181,1182,2640,2641,2644,2645
  hadd dccore.swm p.27.bx 1180,1181
  hadd dccore.swm p.27.fr 232-235
  hadd dccore.swm keys.n 11
  hadd dccore.swm keys.1 SERVER PORT NICKNAME ALT_NICKNAME REJOIN_ATTEMPTS ON_CONNECT_CHECK_MINUTES SEARCH_ENABLED ANNOUNCE_TRANSFERS PRIVATE_MESSAGES_ENABLED WEBUI_ENABLED CTCP_VERSION_REPLY CHECK_FOR_UPDATES DEBUG_CHANNEL CHANNEL ADMIN_NICK
  hadd dccore.swm keys.2 THEME CUSTOM_THEME_BORDER CUSTOM_THEME_SEPARATOR CUSTOM_THEME_TEXTBOX CUSTOM_THEME_VALUE CUSTOM_THEME_ALERT CUSTOM_THEME_ACCENT ANNOUNCE_INTERVAL BROADCAST_SEARCH_CHANNEL BROADCAST_SEARCH_COOLDOWN MSG_DELAY DEBUG_MSG_DELAY LIST_BASE_NAME LIST_FORMAT LIST_IGNORED_EXTENSIONS
  hadd dccore.swm keys.3 SEPARATE_VIDEO_LIST LIST_VIDEO_EXTENSIONS LIST_VIDEO_COMPANION_EXTENSIONS RAR_ENABLED RAR_EXTENSIONS RAR_BINARY MAX_RAR_FOLDER_SIZE RAR_TIMEOUT LIST_HEADER_FILE LIST_HEADER_MAX_BYTES LIST_SHOW_AUDIO_INFO LIST_REBUILD_SCHEDULE LIST_UPDATE_TIMEOUT LIST_UPDATE_STALL_SECONDS LIST_AUDIO_INFO_MINUTES
  hadd dccore.swm keys.4 LIST_AUDIO_INFO_THREADS LIST_SCAN_THREADS MAX_UPLOAD_KB MAX_DOWNLOAD_KB DCC_BLOCK_SIZE DCC_SEND_BUFFER DCC_PORT_START DCC_PORT_END DCC_ACCEPT_TIMEOUT MAX_SEND_FAILS DAILY_SEND_FAILS_LIMIT MAX_DCC_SLOTS LARGE_FILE_MB SLOW_SEND_MIN_KB MAX_USER_QUEUE
  hadd dccore.swm keys.5 MAX_GLOBAL_QUEUE MAX_SEARCH_RESULTS SEARCH_SHOW_FOLDER SEARCH_FOLDER_MAX_CHARS PAUSE_ON_UPDATE PAUSE_FOR_WHOLE_UPDATE REHASH_TRANSFER_WAIT AUTO_GRAB_LISTS AUTO_GRAB_EVERY_MINUTES AUTO_GRAB_MIN_FILES AUTO_GRAB_MIN_SPEED_KB AUTO_DISCOVER_CHANNEL_LISTS MULTI_CHANNEL_LIST_STABLE_SECONDS MAX_FETCH_SLOTS AUTO_REFETCH_LISTS
  hadd dccore.swm keys.6 AUTO_REFETCH_INTERVAL_HOURS AUTO_REFETCH_MAX_PER_RUN FETCH_OFFER_TIMEOUT FETCH_TRANSFER_TIMEOUT FETCH_FOLDER_OFFER_TIMEOUT FETCH_FOLDER_OFFER_TIMEOUT_UNADVERTISED FETCH_FOLDER_TRANSFER_TIMEOUT MAX_FETCH_FILE_SIZE MAX_FETCH_FOLDER_FILE_SIZE MAX_FETCH_LIST_FILE_SIZE MAX_LIST_TEXT_SIZE FETCH_HISTORY_DAYS FETCH_HISTORY_MAX_ROWS FETCH_MAX_PER_BOT FETCH_QUEUED_TIMEOUT
  hadd dccore.swm keys.7 FETCH_BOT_MAX_FAILS FETCH_BOT_COOLDOWN_MINUTES LIST_REQUEST_TAG MAX_REQUESTS REQUEST_WINDOW MUTE_TIME FLOOD_BAN_SECONDS PRIVATE_MESSAGE_COOLDOWN_SECONDS PRIVATE_MESSAGE_DECLINE_TEXT PRIVATE_MESSAGE_DECLINE_INTERVAL_SECONDS PRIVATE_MESSAGE_DECLINE_BURST PRIVATE_MESSAGE_DECLINE_BURST_SECONDS ADMIN_HOSTMASKS ADMIN_CHAT_MODE ADMIN_CHANNEL_COMMANDS
  hadd dccore.swm keys.8 ADMIN_CHAT_COLOURS WEBUI_HOST WEBUI_PORT WEBUI_ALLOWED_HOSTS WEBUI_CONSOLE_ENABLED WEBUI_OPEN_BROWSER WEBUI_FOLDER_BROWSER_ENABLED CONSOLE_SHOW_REQUESTS CONSOLE_SHOW_QUEUE CONSOLE_SHOW_SENDS CONSOLE_SHOW_FAILURES CONSOLE_SHOW_SEARCHES DEBUG_CHANNEL_FEED DEBUG_MODE DEBUG_TO_CHANNEL
  hadd dccore.swm keys.9 DEBUG_TO_CONSOLE CONSOLE_TIMESTAMP_FORMAT CONSOLE_LOG_FILE CONSOLE_LOG_MAX_MB CONSOLE_LOG_KEEP BOT_WINDOW PROJECT_URL TMP_ZIP_DIR LOCAL_LIST_DIR FETCHED_FILES_DIR LIBRARY_FOLDERS_FILE LISTS_FILE BANS_FILE HARD_BANS_FILE STATS_FILE
  hadd dccore.swm keys.10 KNOWN_BOTS_FILE FETCHED_BOT_LISTS_FILE LIST_INDEX_FILE LIST_AUDIO_INFO_CACHE FETCH_HISTORY_FILE DOWNLOAD_COUNTS_FILE TRANSFER_LOG_FILE LIST_SIZE_FILE LIST_RAWBYTES_FILE LIST_PROGRESS_FILE ADMIN_TOKENS_FILE ON_CONNECT_FILE NOTICES_FILE PRIVATE_MESSAGES_FILE DCC_QUEUE_FILE
  hadd dccore.swm keys.11 CHAT_HISTORY_DAYS CHAT_HISTORY_MAX_MB
  hadd dccore.swm k.SERVER 2001 str 1 2000
  hadd dccore.swm pg.SERVER 1
  hadd dccore.swm n.SERVER IRC server
  hadd dccore.swm h.SERVER The IRC server the bot connects to. For Undernet leave it as irc.undernet.org.
  hadd dccore.swm k.PORT 2005 int 1 2004
  hadd dccore.swm pg.PORT 1
  hadd dccore.swm n.PORT Port
  hadd dccore.swm h.PORT The port on that server. 6667 is the normal one for plain IRC; the bot does not use SSL.
  hadd dccore.swm k.NICKNAME 2009 str 1 2008
  hadd dccore.swm pg.NICKNAME 1
  hadd dccore.swm n.NICKNAME Nickname
  hadd dccore.swm h.NICKNAME The bot's name on IRC. People request files with it (for example @YourBot for the list)~2C so pick something short and easy to type. Required - the bot will not start without it.
  hadd dccore.swm k.ALT_NICKNAME 2013 str 1 2012
  hadd dccore.swm pg.ALT_NICKNAME 1
  hadd dccore.swm n.ALT_NICKNAME Alt nickname
  hadd dccore.swm h.ALT_NICKNAME A backup name used if the main one is already taken when the bot connects. If this one is taken too~2C a digit is added to it. The bot switches back to the main name as soon as it is free.
  hadd dccore.swm k.REJOIN_ATTEMPTS 2017 int 1 2016
  hadd dccore.swm pg.REJOIN_ATTEMPTS 1
  hadd dccore.swm n.REJOIN_ATTEMPTS Rejoin attempts after a kick (0 = never)
  hadd dccore.swm h.REJOIN_ATTEMPTS How many times the bot tries to get back into a channel after being kicked before giving up on it. It waits until the next advert is due before each try~2C so it never looks like it is fighting the kick. 0 means never rejoin.
  hadd dccore.swm k.ON_CONNECT_CHECK_MINUTES 2021 int 1 2020
  hadd dccore.swm pg.ON_CONNECT_CHECK_MINUTES 1
  hadd dccore.swm n.ON_CONNECT_CHECK_MINUTES Check the on-connect commands worked every (minutes~2C 0 = never)
  hadd dccore.swm h.ON_CONNECT_CHECK_MINUTES How often the bot checks that its on-connect commands worked. If they set a user mode such as +x and the server has not given it - or~2C for +x~2C has not hidden the host - the bot sends all of them again. This is for a net split~2C when the X login can go nowhere.
  hadd dccore.swm k.SEARCH_ENABLED 2041 bool 1 2041
  hadd dccore.swm pg.SEARCH_ENABLED 2
  hadd dccore.swm n.SEARCH_ENABLED Answer @find searches
  hadd dccore.swm h.SEARCH_ENABLED Answer @find and @locator searches. Off~2C they are ignored without a reply and the channel advert says Search: OFF. Requests for files and lists are still answered.
  hadd dccore.swm k.ANNOUNCE_TRANSFERS 2045 bool 1 2045
  hadd dccore.swm pg.ANNOUNCE_TRANSFERS 2
  hadd dccore.swm n.ANNOUNCE_TRANSFERS Announce finished transfers in the channel
  hadd dccore.swm h.ANNOUNCE_TRANSFERS Post a line in the channel each time a file has been sent. Everything else about a transfer is private to the person who asked; this is the only public part. A file asked for by private message is never announced.
  hadd dccore.swm k.PRIVATE_MESSAGES_ENABLED 2049 bool 1 2049
  hadd dccore.swm pg.PRIVATE_MESSAGES_ENABLED 2
  hadd dccore.swm n.PRIVATE_MESSAGES_ENABLED Keep private messages (off: keep none~2C reply once instead)
  hadd dccore.swm h.PRIVATE_MESSAGES_ENABLED Keep private messages people send to the bot so you can read them on the Messages page. The bot does not answer them. Turn off to keep none and instead reply once telling the sender where to go.
  hadd dccore.swm k.WEBUI_ENABLED 2053 bool 1 2053
  hadd dccore.swm pg.WEBUI_ENABLED 2
  hadd dccore.swm n.WEBUI_ENABLED Enable web dashboard
  hadd dccore.swm h.WEBUI_ENABLED Turn the web dashboard on. Off by default so nothing opens a web page just because the bot was updated. Needs the Flask package installed.
  hadd dccore.swm k.CTCP_VERSION_REPLY 2057 bool 1 2057
  hadd dccore.swm pg.CTCP_VERSION_REPLY 2
  hadd dccore.swm n.CTCP_VERSION_REPLY Answer CTCP VERSION
  hadd dccore.swm h.CTCP_VERSION_REPLY Answer when somebody asks the bot what software it runs (a CTCP VERSION request). The answer goes only to the person who asked. Turn off to stay quiet about it.
  hadd dccore.swm k.CHECK_FOR_UPDATES 2061 bool 1 2061
  hadd dccore.swm pg.CHECK_FOR_UPDATES 2
  hadd dccore.swm n.CHECK_FOR_UPDATES Tell me when a new version is out
  hadd dccore.swm h.CHECK_FOR_UPDATES Once a day~2C ask GitHub whether a newer DCCore has been released~2C and say so on the dashboard~2C in the console and in the mIRC window. Only the version numbers are compared; nothing about your bot is sent. Turn it off on a machine that should not go out.
  hadd dccore.swm k.DEBUG_CHANNEL 2065 str 1 2064
  hadd dccore.swm pg.DEBUG_CHANNEL 3
  hadd dccore.swm n.DEBUG_CHANNEL Debug channel
  hadd dccore.swm h.DEBUG_CHANNEL A channel of your own where the bot reports what it is doing - transfers~2C joins~2C bans~2C problems. Leave blank for none. Do not use a channel other people sit in: everything the bot reports goes there.
  hadd dccore.swm k.CHANNEL 1520 chanlist 1 0
  hadd dccore.swm pg.CHANNEL 3
  hadd dccore.swm n.CHANNEL Channels
  hadd dccore.swm h.CHANNEL The channel(s) the bot serves in~2C each starting with ~23~2C separated by commas. The first one is where announcements go unless a request came from another channel. Required.
  hadd dccore.swm k.ADMIN_NICK 2069 str 1 2068
  hadd dccore.swm pg.ADMIN_NICK 4
  hadd dccore.swm n.ADMIN_NICK Admin nick(s)
  hadd dccore.swm h.ADMIN_NICK Your own nick(s) - the people allowed to use the admin commands such as !ban~2C !rehash and !update. Separate several with commas. Required. If you have set ADMIN_HOSTMASKS~2C the command must also come from that host.
  hadd dccore.swm k.THEME 2073 choice 1 2072
  hadd dccore.swm pg.THEME 5
  hadd dccore.swm n.THEME Colour theme
  hadd dccore.swm ch.THEME classic midnight forest orchid plain
  hadd dccore.swm cl.THEME.1 classic
  hadd dccore.swm cl.THEME.2 midnight
  hadd dccore.swm cl.THEME.3 forest
  hadd dccore.swm cl.THEME.4 orchid
  hadd dccore.swm cl.THEME.5 plain
  hadd dccore.swm h.THEME The colour scheme for everything the bot says in the channel - the advert~2C the notices~2C the search results. Pick one you like; it is how people tell your bot apart from the others.
  hadd dccore.swm k.CUSTOM_THEME_BORDER 2077 colour 1 2076
  hadd dccore.swm pg.CUSTOM_THEME_BORDER 5
  hadd dccore.swm n.CUSTOM_THEME_BORDER Border colour
  hadd dccore.swm h.CUSTOM_THEME_BORDER Override one colour of the chosen theme: the block that frames each message. Leave unset to keep the theme's own colour.
  hadd dccore.swm k.CUSTOM_THEME_SEPARATOR 2081 colour 1 2080
  hadd dccore.swm pg.CUSTOM_THEME_SEPARATOR 5
  hadd dccore.swm n.CUSTOM_THEME_SEPARATOR Separator colour
  hadd dccore.swm h.CUSTOM_THEME_SEPARATOR Override one colour of the chosen theme: the block between the parts of a message.
  hadd dccore.swm k.CUSTOM_THEME_TEXTBOX 2085 colour 1 2084
  hadd dccore.swm pg.CUSTOM_THEME_TEXTBOX 5
  hadd dccore.swm n.CUSTOM_THEME_TEXTBOX Text box colour
  hadd dccore.swm h.CUSTOM_THEME_TEXTBOX Override one colour of the chosen theme: the background the text sits on.
  hadd dccore.swm k.CUSTOM_THEME_VALUE 2089 colour 1 2088
  hadd dccore.swm pg.CUSTOM_THEME_VALUE 5
  hadd dccore.swm n.CUSTOM_THEME_VALUE Value colour
  hadd dccore.swm h.CUSTOM_THEME_VALUE Override one colour of the chosen theme: the numbers and names in a message~2C like a file count or a speed.
  hadd dccore.swm k.CUSTOM_THEME_ALERT 2093 colour 1 2092
  hadd dccore.swm pg.CUSTOM_THEME_ALERT 5
  hadd dccore.swm n.CUSTOM_THEME_ALERT Alert colour
  hadd dccore.swm h.CUSTOM_THEME_ALERT Override one colour of the chosen theme: the parts meant to stand out.
  hadd dccore.swm k.CUSTOM_THEME_ACCENT 2097 colour 1 2096
  hadd dccore.swm pg.CUSTOM_THEME_ACCENT 5
  hadd dccore.swm n.CUSTOM_THEME_ACCENT Accent colour
  hadd dccore.swm h.CUSTOM_THEME_ACCENT Override one colour of the chosen theme: timestamps and secondary text.
  hadd dccore.swm k.ANNOUNCE_INTERVAL 2101 int 1 2100
  hadd dccore.swm pg.ANNOUNCE_INTERVAL 6
  hadd dccore.swm n.ANNOUNCE_INTERVAL Advert interval (seconds)
  hadd dccore.swm h.ANNOUNCE_INTERVAL How often the bot posts its advert in the channel~2C in seconds. 300 is every five minutes. 60 is the least it accepts - channels do not like a bot that advertises constantly.
  hadd dccore.swm k.BROADCAST_SEARCH_CHANNEL 2105 str 1 2104
  hadd dccore.swm pg.BROADCAST_SEARCH_CHANNEL 6
  hadd dccore.swm n.BROADCAST_SEARCH_CHANNEL Broadcast search channel
  hadd dccore.swm h.BROADCAST_SEARCH_CHANNEL The one channel used when you search all bots at once from the dashboard. Leave empty to use your first channel.
  hadd dccore.swm k.BROADCAST_SEARCH_COOLDOWN 2109 int 1 2108
  hadd dccore.swm pg.BROADCAST_SEARCH_COOLDOWN 6
  hadd dccore.swm n.BROADCAST_SEARCH_COOLDOWN Broadcast search cooldown (seconds)
  hadd dccore.swm h.BROADCAST_SEARCH_COOLDOWN How many seconds must pass between two of those search-all-bots searches~2C to be polite to the other bots in the channel.
  hadd dccore.swm k.MSG_DELAY 2113 float 1 2112
  hadd dccore.swm pg.MSG_DELAY 6
  hadd dccore.swm n.MSG_DELAY Message delay (seconds)
  hadd dccore.swm h.MSG_DELAY How many seconds the bot waits between the lines it sends to the server. Protects you from being disconnected for flooding. 5 is safe on Undernet; lower is faster but riskier.
  hadd dccore.swm k.DEBUG_MSG_DELAY 2117 float 1 2116
  hadd dccore.swm pg.DEBUG_MSG_DELAY 6
  hadd dccore.swm n.DEBUG_MSG_DELAY Debug message delay (seconds)
  hadd dccore.swm h.DEBUG_MSG_DELAY How long to wait between lines to your debug channel. Never less than MSG_DELAY - every line the bot sends shares one clock~2C and a smaller number here has no effect.
  hadd dccore.swm k.LIST_BASE_NAME 2121 str 1 2120
  hadd dccore.swm pg.LIST_BASE_NAME 9
  hadd dccore.swm n.LIST_BASE_NAME List base name
  hadd dccore.swm h.LIST_BASE_NAME The name your list files start with (for example DCCore-2026-09-18.txt). Normally the same as the bot's nickname~2C which is what happens if you leave it alone. Letters~2C digits and - _ . ~5B ~5D ~7B ~7D ^ ~60 only~2C so it is a file name on every system.
  hadd dccore.swm k.LIST_FORMAT 2125 choice 1 2124
  hadd dccore.swm pg.LIST_FORMAT 9
  hadd dccore.swm n.LIST_FORMAT List delivery format
  hadd dccore.swm ch.LIST_FORMAT txt zip rar
  hadd dccore.swm cl.LIST_FORMAT.1 txt
  hadd dccore.swm cl.LIST_FORMAT.2 zip
  hadd dccore.swm cl.LIST_FORMAT.3 rar
  hadd dccore.swm h.LIST_FORMAT How the list is sent to somebody who asks for it: as a plain .txt~2C packed as .zip~2C or packed as .rar. Zip is what most people can open. Rar needs the rar program installed.
  hadd dccore.swm k.LIST_IGNORED_EXTENSIONS 2129 list 1 2128
  hadd dccore.swm pg.LIST_IGNORED_EXTENSIONS 9
  hadd dccore.swm n.LIST_IGNORED_EXTENSIONS File types to leave out of the list
  hadd dccore.swm h.LIST_IGNORED_EXTENSIONS File types to leave out of your list~2C separated by commas (for example .db~2C .ini). Everything else under your shared folders is listed and can be downloaded~2C so keep private files out of those folders.
  hadd dccore.swm k.SEPARATE_VIDEO_LIST 2133 bool 1 2133
  hadd dccore.swm pg.SEPARATE_VIDEO_LIST 9
  hadd dccore.swm n.SEPARATE_VIDEO_LIST Separate film and series list
  hadd dccore.swm h.SEPARATE_VIDEO_LIST Put films and series in their own list file~2C separate from the music~2C instead of one list with everything mixed. Both files are sent together when somebody asks for your list.
  hadd dccore.swm k.LIST_VIDEO_EXTENSIONS 2137 list 1 2136
  hadd dccore.swm pg.LIST_VIDEO_EXTENSIONS 9
  hadd dccore.swm n.LIST_VIDEO_EXTENSIONS File types that go in the film list
  hadd dccore.swm h.LIST_VIDEO_EXTENSIONS Which file types count as video and go in the film list (when the separate film list is on). Separated by commas.
  hadd dccore.swm k.LIST_VIDEO_COMPANION_EXTENSIONS 2141 list 1 2140
  hadd dccore.swm pg.LIST_VIDEO_COMPANION_EXTENSIONS 9
  hadd dccore.swm n.LIST_VIDEO_COMPANION_EXTENSIONS File types that follow a film into its list (subtitles~2C .nfo~2C .sfv)
  hadd dccore.swm h.LIST_VIDEO_COMPANION_EXTENSIONS File types that belong to a film and should go in the film list with it - subtitles~2C .nfo~2C .sfv - when they are in the same folder as a video. In a folder with no video (an album) they stay with the music.
  hadd dccore.swm k.RAR_ENABLED 2145 bool 1 2145
  hadd dccore.swm pg.RAR_ENABLED 9
  hadd dccore.swm n.RAR_ENABLED Enable !rar folder packing
  hadd dccore.swm h.RAR_ENABLED Let people request a whole folder packed as one .rar file (with !rar). Turn off if you do not have the rar program or do not want the bot packing folders. Single-file downloads work either way.
  hadd dccore.swm k.RAR_EXTENSIONS 2149 list 1 2148
  hadd dccore.swm pg.RAR_EXTENSIONS 9
  hadd dccore.swm n.RAR_EXTENSIONS File types a folder needs to be !rar-packable
  hadd dccore.swm h.RAR_EXTENSIONS A folder can be requested as a .rar only if it contains one of these file types~2C directly or in its disc folders (CD1~2C Disc 2). The default is music formats~2C so albums can be packed but a folder with one big film cannot.
  hadd dccore.swm k.RAR_BINARY 2153 str 1 2152
  hadd dccore.swm pg.RAR_BINARY 9
  hadd dccore.swm n.RAR_BINARY RAR binary path
  hadd dccore.swm h.RAR_BINARY Where the rar program is on this machine. Leave empty and the bot finds it by itself (on the PATH~2C or in WinRAR's folder on Windows). It also opens a list another bot sends as .rar; without it such a list is refused.
  hadd dccore.swm k.MAX_RAR_FOLDER_SIZE 2157 int 1048576 2156
  hadd dccore.swm pg.MAX_RAR_FOLDER_SIZE 9
  hadd dccore.swm n.MAX_RAR_FOLDER_SIZE Largest folder !rar will pack (0 = no limit) (MB)
  hadd dccore.swm h.MAX_RAR_FOLDER_SIZE The biggest folder the bot will pack as a .rar~2C in bytes. Stops somebody asking for a folder of hundreds of gigabytes. 10 GB fits any album or box set; 0 means no limit.
  hadd dccore.swm k.RAR_TIMEOUT 2161 int 1 2160
  hadd dccore.swm pg.RAR_TIMEOUT 9
  hadd dccore.swm n.RAR_TIMEOUT RAR pack timeout (seconds)
  hadd dccore.swm h.RAR_TIMEOUT How many seconds a folder may take to pack before the bot gives up on it.
  hadd dccore.swm k.LIST_HEADER_FILE 2165 str 1 2164
  hadd dccore.swm pg.LIST_HEADER_FILE 9
  hadd dccore.swm n.LIST_HEADER_FILE List banner file
  hadd dccore.swm h.LIST_HEADER_FILE A text file whose contents are printed at the top of your list - a greeting~2C your channel name~2C some ASCII art. If the file does not exist~2C nothing is added.
  hadd dccore.swm k.LIST_HEADER_MAX_BYTES 2169 int 1024 2168
  hadd dccore.swm pg.LIST_HEADER_MAX_BYTES 9
  hadd dccore.swm n.LIST_HEADER_MAX_BYTES List banner size limit (KB)
  hadd dccore.swm h.LIST_HEADER_MAX_BYTES The most of that file that will be used~2C in bytes~2C so a wrong file cannot bloat every list.
  hadd dccore.swm k.LIST_SHOW_AUDIO_INFO 2173 bool 1 2173
  hadd dccore.swm pg.LIST_SHOW_AUDIO_INFO 9
  hadd dccore.swm n.LIST_SHOW_AUDIO_INFO Length and quality in the list
  hadd dccore.swm h.LIST_SHOW_AUDIO_INFO Add each MP3 and FLAC file's length and quality after its size in your list~2C e.g. 10.3MB 4m31s 320/44.1/JS. Every audio file is read once~2C in the background after the list is published; searches and downloads pause only for the seconds the lengths take to swap in.
  hadd dccore.swm k.LIST_REBUILD_SCHEDULE 2177 str 1 2176
  hadd dccore.swm pg.LIST_REBUILD_SCHEDULE 10
  hadd dccore.swm n.LIST_REBUILD_SCHEDULE Rebuild the list automatically
  hadd dccore.swm h.LIST_REBUILD_SCHEDULE Rebuild the list by itself~2C the same way !update does. Write daily 04:00~2C weekly sun 04:00~2C monthly 1 03:30 or every 12h (hours since the last rebuild~2C including yours). Empty: only when you ask. The bot's own clock; if it was off at that time~2C it rebuilds when it starts again.
  hadd dccore.swm k.LIST_UPDATE_TIMEOUT 2181 int 1 2180
  hadd dccore.swm pg.LIST_UPDATE_TIMEOUT 10
  hadd dccore.swm n.LIST_UPDATE_TIMEOUT List rebuild hard cap (seconds~2C 0 = none)
  hadd dccore.swm h.LIST_UPDATE_TIMEOUT A hard limit in seconds on how long a list rebuild may run. 0 means no limit~2C which is the right choice: a huge library can genuinely take hours~2C and the setting below already catches a rebuild that has stopped doing anything.
  hadd dccore.swm k.LIST_UPDATE_STALL_SECONDS 2185 int 1 2184
  hadd dccore.swm pg.LIST_UPDATE_STALL_SECONDS 10
  hadd dccore.swm n.LIST_UPDATE_STALL_SECONDS Give up if a rebuild reports nothing for (seconds)
  hadd dccore.swm h.LIST_UPDATE_STALL_SECONDS If a list rebuild reports no progress for this many seconds~2C it is treated as stuck and stopped. 15 minutes is generous on purpose so a slow network drive is not cut off. A silent background audio reading is stopped the same way~2C keeping what it read.
  hadd dccore.swm k.LIST_AUDIO_INFO_MINUTES 2189 int 1 2188
  hadd dccore.swm pg.LIST_AUDIO_INFO_MINUTES 10
  hadd dccore.swm n.LIST_AUDIO_INFO_MINUTES Time limit for reading audio files (no longer used)
  hadd dccore.swm h.LIST_AUDIO_INFO_MINUTES No longer used. Audio files are now read after the list is published~2C in the background and with no time limit~2C so nothing waits for them. Kept only so an older settings file still loads.
  hadd dccore.swm k.LIST_AUDIO_INFO_THREADS 2193 int 1 2192
  hadd dccore.swm pg.LIST_AUDIO_INFO_THREADS 10
  hadd dccore.swm n.LIST_AUDIO_INFO_THREADS Audio files read at once
  hadd dccore.swm h.LIST_AUDIO_INFO_THREADS How many audio files are read at once for their length and quality. On a network drive most of the time is waiting~2C so this is 64 by default; lower it only if a slow or small setup does not benefit. The reading says the rate it got~2C to compare. 1 to 128.
  hadd dccore.swm k.LIST_SCAN_THREADS 2197 int 1 2196
  hadd dccore.swm pg.LIST_SCAN_THREADS 10
  hadd dccore.swm n.LIST_SCAN_THREADS Folders scanned at once
  hadd dccore.swm h.LIST_SCAN_THREADS How many folders are listed at once while the list is rebuilt. On a network drive most of the time is waiting~2C so more at once makes every rebuild shorter. With Pause for the whole rebuild on~2C searches wait while it runs. 1 lists one folder at a time. 1 to 64.
  hadd dccore.swm k.MAX_UPLOAD_KB 2201 int 1 2200
  hadd dccore.swm pg.MAX_UPLOAD_KB 11
  hadd dccore.swm n.MAX_UPLOAD_KB Max upload speed (KB/s~2C 0 = no limit)
  hadd dccore.swm h.MAX_UPLOAD_KB The most all your sends together may use~2C in KB/s~2C so sharing leaves room on your line. Shared: three sends under 600 get about 200 each~2C and the others speed up when one ends. A change applies at once~2C to sends already running too. 0 is no limit.
  hadd dccore.swm k.MAX_DOWNLOAD_KB 2205 int 1 2204
  hadd dccore.swm pg.MAX_DOWNLOAD_KB 11
  hadd dccore.swm n.MAX_DOWNLOAD_KB Max download speed (KB/s~2C 0 = no limit)
  hadd dccore.swm h.MAX_DOWNLOAD_KB The most everything the bot fetches may use together~2C in KB/s: list fetches~2C automatic grabs~2C files and folders from other bots. Waiting on this limit does not count toward the fetch timeouts~2C so a big list is not cut off for being slow.
  hadd dccore.swm k.DCC_BLOCK_SIZE 2209 choice 1 2208
  hadd dccore.swm pg.DCC_BLOCK_SIZE 11
  hadd dccore.swm n.DCC_BLOCK_SIZE Packet size
  hadd dccore.swm ch.DCC_BLOCK_SIZE 4096 8192 16384 32768 65536 131072 262144
  hadd dccore.swm cl.DCC_BLOCK_SIZE.1 4 KB
  hadd dccore.swm cl.DCC_BLOCK_SIZE.2 8 KB
  hadd dccore.swm cl.DCC_BLOCK_SIZE.3 16 KB
  hadd dccore.swm cl.DCC_BLOCK_SIZE.4 32 KB
  hadd dccore.swm cl.DCC_BLOCK_SIZE.5 64 KB
  hadd dccore.swm cl.DCC_BLOCK_SIZE.6 128 KB
  hadd dccore.swm cl.DCC_BLOCK_SIZE.7 256 KB
  hadd dccore.swm h.DCC_BLOCK_SIZE How much the bot sends at once~2C in bytes. The default~2C 64 KB~2C suits most bots.
  hadd dccore.swm k.DCC_SEND_BUFFER 2213 int 1024 2212
  hadd dccore.swm pg.DCC_SEND_BUFFER 11
  hadd dccore.swm n.DCC_SEND_BUFFER Socket send buffer (0 = the default for your platform) (KB)
  hadd dccore.swm h.DCC_SEND_BUFFER How much data the operating system may hold in flight for one send~2C in bytes. 0 uses the platform default: 4 MB on Windows~2C which would otherwise hold only 64 KB~2C and the system's own tuning on Linux and macOS - right for nearly every connection.
  hadd dccore.swm k.DCC_PORT_START 2217 int 1 2216
  hadd dccore.swm pg.DCC_PORT_START 11
  hadd dccore.swm n.DCC_PORT_START DCC port range start
  hadd dccore.swm h.DCC_PORT_START The first port the bot listens on when sending a file. If you are behind a router~2C forward this whole range (start to end) to the machine running the bot~2C or nobody can download from you.
  hadd dccore.swm k.DCC_PORT_END 2221 int 1 2220
  hadd dccore.swm pg.DCC_PORT_END 11
  hadd dccore.swm n.DCC_PORT_END DCC port range end
  hadd dccore.swm h.DCC_PORT_END The last port of that range. Sends~2C downloads from other bots and a listen-mode admin console all take their ports from it~2C so keep at least as many ports as your send slots and fetch slots together~2C plus one.
  hadd dccore.swm k.DCC_ACCEPT_TIMEOUT 2225 int 1 2224
  hadd dccore.swm pg.DCC_ACCEPT_TIMEOUT 11
  hadd dccore.swm n.DCC_ACCEPT_TIMEOUT Wait for the receiver to connect (seconds)
  hadd dccore.swm h.DCC_ACCEPT_TIMEOUT How many seconds the bot waits for someone to accept a file it has offered before withdrawing the offer and counting one failed attempt. A person who has to click Accept in a dialog often needs more than the default 30.
  hadd dccore.swm k.MAX_SEND_FAILS 2229 int 1 2228
  hadd dccore.swm pg.MAX_SEND_FAILS 11
  hadd dccore.swm n.MAX_SEND_FAILS Max send failures
  hadd dccore.swm h.MAX_SEND_FAILS How many times the bot retries sending one queued file if the download does not connect or fails~2C before dropping it from the queue and telling the person.
  hadd dccore.swm k.DAILY_SEND_FAILS_LIMIT 2233 int 1 2232
  hadd dccore.swm pg.DAILY_SEND_FAILS_LIMIT 11
  hadd dccore.swm n.DAILY_SEND_FAILS_LIMIT Failed sends in a day before a nick is paused (0 = off)
  hadd dccore.swm h.DAILY_SEND_FAILS_LIMIT 0 means off.
  hadd dccore.swm k.MAX_DCC_SLOTS 2237 int 1 2236
  hadd dccore.swm pg.MAX_DCC_SLOTS 12
  hadd dccore.swm n.MAX_DCC_SLOTS Max simultaneous sends
  hadd dccore.swm h.MAX_DCC_SLOTS How many files the bot sends at the same time. Everyone else waits in the queue. 3 is a good number for a home connection; raise it only if your upload speed can take it.
  hadd dccore.swm k.LARGE_FILE_MB 2241 int 1 2240
  hadd dccore.swm pg.LARGE_FILE_MB 12
  hadd dccore.swm n.LARGE_FILE_MB One large file from (MB~2C 0 = off)
  hadd dccore.swm h.LARGE_FILE_MB Files of at least this many MB count as large~2C and only one is sent at a time~2C so a few huge downloads cannot hold every slot for hours. Someone whose next file is large keeps their place and waits; the slot goes to the next in line.
  hadd dccore.swm k.SLOW_SEND_MIN_KB 2245 int 1 2244
  hadd dccore.swm pg.SLOW_SEND_MIN_KB 12
  hadd dccore.swm n.SLOW_SEND_MIN_KB Stop sends slower than (KB/s~2C 0 = off)
  hadd dccore.swm h.SLOW_SEND_MIN_KB Only while every slot is taken and someone is waiting. 0 means off. The slowest send~2C under this many KB/s over its last minute while another send manages it~2C is stopped for the next in line. A queued file stays queued to resume; one sent without waiting is dropped.
  hadd dccore.swm k.MAX_USER_QUEUE 2249 int 1 2248
  hadd dccore.swm pg.MAX_USER_QUEUE 12
  hadd dccore.swm n.MAX_USER_QUEUE Max queue per user
  hadd dccore.swm h.MAX_USER_QUEUE The most files one person can have waiting in their queue at once.
  hadd dccore.swm k.MAX_GLOBAL_QUEUE 2253 int 1 2252
  hadd dccore.swm pg.MAX_GLOBAL_QUEUE 12
  hadd dccore.swm n.MAX_GLOBAL_QUEUE Max global queue
  hadd dccore.swm h.MAX_GLOBAL_QUEUE The most files that can be waiting across everybody's queues put together.
  hadd dccore.swm k.MAX_SEARCH_RESULTS 2257 int 1 2256
  hadd dccore.swm pg.MAX_SEARCH_RESULTS 12
  hadd dccore.swm n.MAX_SEARCH_RESULTS Max search results
  hadd dccore.swm h.MAX_SEARCH_RESULTS How many matching files are sent back to somebody who searches with @find. Each result is one line to that person.
  hadd dccore.swm k.SEARCH_SHOW_FOLDER 2261 bool 1 2261
  hadd dccore.swm pg.SEARCH_SHOW_FOLDER 12
  hadd dccore.swm n.SEARCH_SHOW_FOLDER Name the folder in search replies
  hadd dccore.swm h.SEARCH_SHOW_FOLDER In the reply to an @find~2C name each result's folder on a line of its own above its files~2C e.g. From: D:\MEDIA\Rock\Some Band\1999 - Some Album. One extra line per folder~2C not per file; the result lines stay exactly as they are.
  hadd dccore.swm k.SEARCH_FOLDER_MAX_CHARS 2265 int 1 2264
  hadd dccore.swm pg.SEARCH_FOLDER_MAX_CHARS 12
  hadd dccore.swm n.SEARCH_FOLDER_MAX_CHARS Longest folder shown (characters)
  hadd dccore.swm h.SEARCH_FOLDER_MAX_CHARS The longest folder a From: line shows~2C in characters~2C so the line does not wrap. A longer one is cut from the left and starts with ...~2C keeping its end~2C where the album name is. At least 10.
  hadd dccore.swm k.PAUSE_ON_UPDATE 2269 bool 1 2269
  hadd dccore.swm pg.PAUSE_ON_UPDATE 12
  hadd dccore.swm n.PAUSE_ON_UPDATE Pause sharing during !update
  hadd dccore.swm h.PAUSE_ON_UPDATE While a rebuilt list is being swapped in - a few seconds at the end of !update - refuse searches and file requests. The rest of the rebuild~2C they are answered from the current list~2C which stays complete until the swap.
  hadd dccore.swm k.PAUSE_FOR_WHOLE_UPDATE 2273 bool 1 2273
  hadd dccore.swm pg.PAUSE_FOR_WHOLE_UPDATE 12
  hadd dccore.swm n.PAUSE_FOR_WHOLE_UPDATE Pause for the whole rebuild
  hadd dccore.swm h.PAUSE_FOR_WHOLE_UPDATE The old behaviour: refuse searches and file requests for the whole rebuild~2C not only while the new list is swapped in. Only needs Pause sharing during !update on too.
  hadd dccore.swm k.REHASH_TRANSFER_WAIT 2277 int 1 2276
  hadd dccore.swm pg.REHASH_TRANSFER_WAIT 12
  hadd dccore.swm n.REHASH_TRANSFER_WAIT Seconds a rehash waits for transfers to finish
  hadd dccore.swm h.REHASH_TRANSFER_WAIT When you rehash (reload settings)~2C how many seconds the bot waits for running transfers to finish first before reloading anyway. 0 reloads straight away.
  hadd dccore.swm k.AUTO_GRAB_LISTS 2281 bool 1 2281
  hadd dccore.swm pg.AUTO_GRAB_LISTS 13
  hadd dccore.swm n.AUTO_GRAB_LISTS Grab the lists of bots you have no list from
  hadd dccore.swm h.AUTO_GRAB_LISTS Fetch the list of each bot that advertises one you do not have~2C one at a time: after a random wait~2C not if someone else just asked that bot~2C and at most 3 tries per bot. A list you remove is not fetched again.
  hadd dccore.swm k.AUTO_GRAB_EVERY_MINUTES 2285 int 1 2284
  hadd dccore.swm pg.AUTO_GRAB_EVERY_MINUTES 13
  hadd dccore.swm n.AUTO_GRAB_EVERY_MINUTES Minutes between automatic grabs
  hadd dccore.swm h.AUTO_GRAB_EVERY_MINUTES The least time between two automatic list grabs~2C in minutes.
  hadd dccore.swm k.AUTO_GRAB_MIN_FILES 2289 int 1 2288
  hadd dccore.swm pg.AUTO_GRAB_MIN_FILES 13
  hadd dccore.swm n.AUTO_GRAB_MIN_FILES Skip bots with fewer files than
  hadd dccore.swm h.AUTO_GRAB_MIN_FILES Do not grab the list of a bot that advertises fewer files than this. 0 grabs any size.
  hadd dccore.swm k.AUTO_GRAB_MIN_SPEED_KB 2293 int 1 2292
  hadd dccore.swm pg.AUTO_GRAB_MIN_SPEED_KB 13
  hadd dccore.swm n.AUTO_GRAB_MIN_SPEED_KB Skip bots slower than (KB/s)
  hadd dccore.swm h.AUTO_GRAB_MIN_SPEED_KB Do not grab the list of a bot that advertises a speed below this~2C in KB/s. A bot that shows no speed is not skipped. 0 turns this off.
  hadd dccore.swm k.AUTO_DISCOVER_CHANNEL_LISTS 2297 bool 1 2297
  hadd dccore.swm pg.AUTO_DISCOVER_CHANNEL_LISTS 13
  hadd dccore.swm n.AUTO_DISCOVER_CHANNEL_LISTS Discover a bot's other channel-bound lists
  hadd dccore.swm h.AUTO_DISCOVER_CHANNEL_LISTS Watch bots you already hold a list from for a second~2C genuinely different list bound to another of your channels~2C and fetch and hold that one too - never instead of the first. Only acts once the difference has held steady for the time below.
  hadd dccore.swm k.MULTI_CHANNEL_LIST_STABLE_SECONDS 2301 int 1 2300
  hadd dccore.swm pg.MULTI_CHANNEL_LIST_STABLE_SECONDS 13
  hadd dccore.swm n.MULTI_CHANNEL_LIST_STABLE_SECONDS Hold stable this long first (seconds)
  hadd dccore.swm h.MULTI_CHANNEL_LIST_STABLE_SECONDS How long~2C in seconds~2C a bot's channels must show a stable~2C differing file count or list date before the setting above acts on it. A bot mid-scan in one channel when its advert goes out should not be mistaken for a second list.
  hadd dccore.swm k.MAX_FETCH_SLOTS 2305 int 1 2304
  hadd dccore.swm pg.MAX_FETCH_SLOTS 14
  hadd dccore.swm n.MAX_FETCH_SLOTS Max fetch slots
  hadd dccore.swm h.MAX_FETCH_SLOTS How many downloads FROM other bots you run at the same time. Separate from your own send slots~2C so your downloading never takes slots away from people downloading from you.
  hadd dccore.swm k.AUTO_REFETCH_LISTS 2309 bool 1 2309
  hadd dccore.swm pg.AUTO_REFETCH_LISTS 14
  hadd dccore.swm n.AUTO_REFETCH_LISTS Re-fetch when a bot has a new list
  hadd dccore.swm h.AUTO_REFETCH_LISTS When another bot advertises that its list has changed~2C fetch the new list automatically. A list from a bot whose advert shows no date is fetched again once it is 14 days old. Off by default because it uses the other bot's bandwidth without you asking each time.
  hadd dccore.swm k.AUTO_REFETCH_INTERVAL_HOURS 2313 int 1 2312
  hadd dccore.swm pg.AUTO_REFETCH_INTERVAL_HOURS 14
  hadd dccore.swm n.AUTO_REFETCH_INTERVAL_HOURS Least time between re-fetches of one bot (hours)
  hadd dccore.swm h.AUTO_REFETCH_INTERVAL_HOURS The least time between two automatic asks for the same bot's list~2C in hours. Counted from the last list that arrived or the last time the bot was asked~2C so a bot that rebuilds hourly - or does not answer - is not asked every hour.
  hadd dccore.swm k.AUTO_REFETCH_MAX_PER_RUN 2317 int 1 2316
  hadd dccore.swm pg.AUTO_REFETCH_MAX_PER_RUN 14
  hadd dccore.swm n.AUTO_REFETCH_MAX_PER_RUN Most lists to re-fetch in one sweep
  hadd dccore.swm h.AUTO_REFETCH_MAX_PER_RUN The most lists to re-fetch in one go. If many are out of date at once~2C the rest are picked up on later rounds~2C oldest first.
  hadd dccore.swm k.FETCH_OFFER_TIMEOUT 2321 int 1 2320
  hadd dccore.swm pg.FETCH_OFFER_TIMEOUT 14
  hadd dccore.swm n.FETCH_OFFER_TIMEOUT Wait for a reply to a fetch request (seconds)
  hadd dccore.swm h.FETCH_OFFER_TIMEOUT When you request a file from another bot~2C how many seconds to wait for it to offer the file before giving up.
  hadd dccore.swm k.FETCH_TRANSFER_TIMEOUT 2325 int 1 2324
  hadd dccore.swm pg.FETCH_TRANSFER_TIMEOUT 14
  hadd dccore.swm n.FETCH_TRANSFER_TIMEOUT Fetch transfer timeout (seconds)
  hadd dccore.swm h.FETCH_TRANSFER_TIMEOUT The longest a file download from another bot may take in total~2C in seconds~2C before it is abandoned.
  hadd dccore.swm k.FETCH_FOLDER_OFFER_TIMEOUT 2329 int 1 2328
  hadd dccore.swm pg.FETCH_FOLDER_OFFER_TIMEOUT 14
  hadd dccore.swm n.FETCH_FOLDER_OFFER_TIMEOUT Wait for a reply to a folder (.rar) request (seconds)
  hadd dccore.swm h.FETCH_FOLDER_OFFER_TIMEOUT When you request a whole folder (.rar) from another bot~2C how many seconds to wait for its offer. Much longer than for a single file~2C because the other bot has to pack the folder first.
  hadd dccore.swm k.FETCH_FOLDER_OFFER_TIMEOUT_UNADVERTISED 2333 int 1 2332
  hadd dccore.swm pg.FETCH_FOLDER_OFFER_TIMEOUT_UNADVERTISED 14
  hadd dccore.swm n.FETCH_FOLDER_OFFER_TIMEOUT_UNADVERTISED ...from a bot that publishes no .rar list (seconds)
  hadd dccore.swm h.FETCH_FOLDER_OFFER_TIMEOUT_UNADVERTISED The same wait~2C but for a bot that does not publish a folder list and probably cannot pack at all - shorter~2C so a slot is not held for half an hour waiting for nothing.
  hadd dccore.swm k.FETCH_FOLDER_TRANSFER_TIMEOUT 2337 int 1 2336
  hadd dccore.swm pg.FETCH_FOLDER_TRANSFER_TIMEOUT 14
  hadd dccore.swm n.FETCH_FOLDER_TRANSFER_TIMEOUT Folder (.rar) fetch transfer timeout (seconds)
  hadd dccore.swm h.FETCH_FOLDER_TRANSFER_TIMEOUT The longest a folder (.rar) download from another bot may take in total~2C in seconds. Larger than the single-file limit because a packed discography is much bigger.
  hadd dccore.swm k.MAX_FETCH_FILE_SIZE 2341 int 1048576 2340
  hadd dccore.swm pg.MAX_FETCH_FILE_SIZE 14
  hadd dccore.swm n.MAX_FETCH_FILE_SIZE Max fetch file size (MB)
  hadd dccore.swm h.MAX_FETCH_FILE_SIZE The biggest single file you will accept from another bot~2C in bytes. Anything larger is refused before the download starts. 0 means no limit - except that a fetched list still may not unpack to more than 8 lists of the biggest size allowed~2C so a booby-trapped list cannot fill your disk.
  hadd dccore.swm k.MAX_FETCH_FOLDER_FILE_SIZE 2345 int 1048576 2344
  hadd dccore.swm pg.MAX_FETCH_FOLDER_FILE_SIZE 14
  hadd dccore.swm n.MAX_FETCH_FOLDER_FILE_SIZE Max folder (.rar) fetch size (MB)
  hadd dccore.swm h.MAX_FETCH_FOLDER_FILE_SIZE The biggest packed folder (.rar) you will accept from another bot~2C in bytes. 0 means no limit.
  hadd dccore.swm k.MAX_FETCH_LIST_FILE_SIZE 2349 int 1048576 2348
  hadd dccore.swm pg.MAX_FETCH_LIST_FILE_SIZE 14
  hadd dccore.swm n.MAX_FETCH_LIST_FILE_SIZE Max fetched master-list zip size (MB)
  hadd dccore.swm h.MAX_FETCH_LIST_FILE_SIZE The biggest list archive you will accept from another bot~2C in bytes. A real list is a few megabytes; this stops a bad offer sending you something huge. 0 means no limit.
  hadd dccore.swm k.MAX_LIST_TEXT_SIZE 2353 int 1048576 2352
  hadd dccore.swm pg.MAX_LIST_TEXT_SIZE 14
  hadd dccore.swm n.MAX_LIST_TEXT_SIZE Largest list text accepted from a peer (MB)
  hadd dccore.swm h.MAX_LIST_TEXT_SIZE The biggest unpacked list you will read from another bot~2C in bytes. Every line of it is kept in memory~2C so this is a memory limit. 0 uses the default.
  hadd dccore.swm k.FETCH_HISTORY_DAYS 2357 int 1 2356
  hadd dccore.swm pg.FETCH_HISTORY_DAYS 14
  hadd dccore.swm n.FETCH_HISTORY_DAYS Keep finished downloads for (days)
  hadd dccore.swm h.FETCH_HISTORY_DAYS How many days a finished download from another bot stays in the Downloads table. The downloaded file itself is kept regardless.
  hadd dccore.swm k.FETCH_HISTORY_MAX_ROWS 2361 int 1 2360
  hadd dccore.swm pg.FETCH_HISTORY_MAX_ROWS 14
  hadd dccore.swm n.FETCH_HISTORY_MAX_ROWS Maximum finished downloads kept
  hadd dccore.swm h.FETCH_HISTORY_MAX_ROWS The most finished downloads kept in that table~2C whatever their age.
  hadd dccore.swm k.FETCH_MAX_PER_BOT 2365 int 1 2364
  hadd dccore.swm pg.FETCH_MAX_PER_BOT 15
  hadd dccore.swm n.FETCH_MAX_PER_BOT Files asked of one bot at once
  hadd dccore.swm h.FETCH_MAX_PER_BOT How many files to ask one bot for at once. The next one is asked when one arrives. Servers allow each person only a few; asking for more gets "queue full". 0 means no limit.
  hadd dccore.swm k.FETCH_QUEUED_TIMEOUT 2369 int 1 2368
  hadd dccore.swm pg.FETCH_QUEUED_TIMEOUT 15
  hadd dccore.swm n.FETCH_QUEUED_TIMEOUT Wait for a queued request (s)
  hadd dccore.swm h.FETCH_QUEUED_TIMEOUT When another bot puts your request in its queue~2C how long to wait for your turn before giving up. Busy servers take hours. 0 waits for ever.
  hadd dccore.swm k.FETCH_BOT_MAX_FAILS 2373 int 1 2372
  hadd dccore.swm pg.FETCH_BOT_MAX_FAILS 15
  hadd dccore.swm n.FETCH_BOT_MAX_FAILS Failed requests in a row that pause a bot
  hadd dccore.swm h.FETCH_BOT_MAX_FAILS How many of your requests to one bot may fail in a row - no answer~2C a connection that could not be made~2C a download that broke off - before that bot is paused for a while.
  hadd dccore.swm k.FETCH_BOT_COOLDOWN_MINUTES 2377 int 1 2376
  hadd dccore.swm pg.FETCH_BOT_COOLDOWN_MINUTES 15
  hadd dccore.swm n.FETCH_BOT_COOLDOWN_MINUTES Minutes a failing bot stays paused
  hadd dccore.swm h.FETCH_BOT_COOLDOWN_MINUTES How many minutes such a bot stays paused. Its requests wait~2C and go out by themselves when the time is up - also after a restart. The Downloads page shows until when~2C with a Resume now button. 0 turns the pause off.
  hadd dccore.swm k.LIST_REQUEST_TAG 2381 str 1 2380
  hadd dccore.swm pg.LIST_REQUEST_TAG 15
  hadd dccore.swm n.LIST_REQUEST_TAG Tag on your list requests
  hadd dccore.swm h.LIST_REQUEST_TAG A short tag added after your list requests~2C the way AutoQ adds ::AutoQ::~2C so a channel can see what is fetching. It is sent only to other DCCore bots~2C which always accept it; other bots get the plain request~2C because many answer only that exact line.
  hadd dccore.swm k.MAX_REQUESTS 2385 int 1 2384
  hadd dccore.swm pg.MAX_REQUESTS 16
  hadd dccore.swm n.MAX_REQUESTS Max commands per window
  hadd dccore.swm h.MAX_REQUESTS How many commands - searches~2C queue checks and the like - one person may send within the time window below before they are muted. File requests are not counted: a pasted list is taken one by one~2C up to the queue limits.
  hadd dccore.swm k.REQUEST_WINDOW 2389 int 1 2388
  hadd dccore.swm pg.REQUEST_WINDOW 16
  hadd dccore.swm n.REQUEST_WINDOW Request window (seconds)
  hadd dccore.swm h.REQUEST_WINDOW The length of that time window~2C in seconds.
  hadd dccore.swm k.MUTE_TIME 2393 int 1 2392
  hadd dccore.swm pg.MUTE_TIME 16
  hadd dccore.swm n.MUTE_TIME Mute duration (seconds)
  hadd dccore.swm h.MUTE_TIME How many seconds somebody is ignored after their first flood.
  hadd dccore.swm k.FLOOD_BAN_SECONDS 2397 int 1 2396
  hadd dccore.swm pg.FLOOD_BAN_SECONDS 16
  hadd dccore.swm n.FLOOD_BAN_SECONDS Ban after flooding while muted (seconds)
  hadd dccore.swm h.FLOOD_BAN_SECONDS How many seconds somebody is banned if they keep flooding while already muted.
  hadd dccore.swm k.PRIVATE_MESSAGE_COOLDOWN_SECONDS 2401 int 1 2400
  hadd dccore.swm pg.PRIVATE_MESSAGE_COOLDOWN_SECONDS 18
  hadd dccore.swm n.PRIVATE_MESSAGE_COOLDOWN_SECONDS Record one private message per sender every (seconds)
  hadd dccore.swm h.PRIVATE_MESSAGE_COOLDOWN_SECONDS After recording a message from somebody~2C ignore further messages from them for this many seconds~2C so one person cannot fill the page.
  hadd dccore.swm k.PRIVATE_MESSAGE_DECLINE_TEXT 2405 str 1 2404
  hadd dccore.swm pg.PRIVATE_MESSAGE_DECLINE_TEXT 18
  hadd dccore.swm n.PRIVATE_MESSAGE_DECLINE_TEXT That reply's wording (~25admin becomes the admin nick)
  hadd dccore.swm h.PRIVATE_MESSAGE_DECLINE_TEXT The one reply sent when private messages are turned off. ~25admin is replaced by your admin nick. Leave blank to send nothing at all.
  hadd dccore.swm k.PRIVATE_MESSAGE_DECLINE_INTERVAL_SECONDS 2409 int 1 2408
  hadd dccore.swm pg.PRIVATE_MESSAGE_DECLINE_INTERVAL_SECONDS 18
  hadd dccore.swm n.PRIVATE_MESSAGE_DECLINE_INTERVAL_SECONDS Reply to the same sender once every (seconds)
  hadd dccore.swm h.PRIVATE_MESSAGE_DECLINE_INTERVAL_SECONDS How many seconds before the same person can get that reply again. One day by default - the reply is there so they learn where to go~2C not to repeat itself.
  hadd dccore.swm k.PRIVATE_MESSAGE_DECLINE_BURST 2413 int 1 2412
  hadd dccore.swm pg.PRIVATE_MESSAGE_DECLINE_BURST 18
  hadd dccore.swm n.PRIVATE_MESSAGE_DECLINE_BURST Most replies to send in one burst window
  hadd dccore.swm h.PRIVATE_MESSAGE_DECLINE_BURST The most of those replies to send in one burst window~2C across everybody. Stops a wave of messages from turning into a wave of replies that delays the transfers people are waiting on.
  hadd dccore.swm k.PRIVATE_MESSAGE_DECLINE_BURST_SECONDS 2417 int 1 2416
  hadd dccore.swm pg.PRIVATE_MESSAGE_DECLINE_BURST_SECONDS 18
  hadd dccore.swm n.PRIVATE_MESSAGE_DECLINE_BURST_SECONDS How long that burst window is (seconds)
  hadd dccore.swm h.PRIVATE_MESSAGE_DECLINE_BURST_SECONDS The length of that burst window~2C in seconds.
  hadd dccore.swm k.ADMIN_HOSTMASKS 2421 list 1 2420
  hadd dccore.swm pg.ADMIN_HOSTMASKS 19
  hadd dccore.swm n.ADMIN_HOSTMASKS Admin hostmasks
  hadd dccore.swm h.ADMIN_HOSTMASKS Who may open the admin console over DCC chat~2C by host. On Undernet~2C log in to X with mode +x and use your yourname.users.undernet.org host - only you can have it. Empty means the console is off. Keep this in admin_config.py rather than here.
  hadd dccore.swm k.ADMIN_CHAT_MODE 2425 choice 1 2424
  hadd dccore.swm pg.ADMIN_CHAT_MODE 19
  hadd dccore.swm n.ADMIN_CHAT_MODE DCC chat connection mode
  hadd dccore.swm ch.ADMIN_CHAT_MODE auto listen connect
  hadd dccore.swm cl.ADMIN_CHAT_MODE.1 auto
  hadd dccore.swm cl.ADMIN_CHAT_MODE.2 listen
  hadd dccore.swm cl.ADMIN_CHAT_MODE.3 connect
  hadd dccore.swm h.ADMIN_CHAT_MODE How the admin console's DCC chat is connected. Auto is right for most people. Choose Listen if you are behind a VPN or a router that does not forward ports~2C so the bot waits for you instead of trying to reach you.
  hadd dccore.swm k.ADMIN_CHANNEL_COMMANDS 2429 bool 1 2429
  hadd dccore.swm pg.ADMIN_CHANNEL_COMMANDS 19
  hadd dccore.swm n.ADMIN_CHANNEL_COMMANDS Allow admin commands in channel
  hadd dccore.swm h.ADMIN_CHANNEL_COMMANDS Let the admin commands (!ban~2C !rehash~2C !update...) also work when you type them in the channel or a private message~2C not only in the console. With ADMIN_HOSTMASKS set they need your host as well as your nick; without it a stolen nick can run them~2C so turn this off.
  hadd dccore.swm k.ADMIN_CHAT_COLOURS 2433 bool 1 2433
  hadd dccore.swm pg.ADMIN_CHAT_COLOURS 19
  hadd dccore.swm n.ADMIN_CHAT_COLOURS Colour the tags in the admin DCC chat
  hadd dccore.swm h.ADMIN_CHAT_COLOURS Colour the ~5BSENT~5D~2C ~5BFAIL~5D~2C ~5BREQUEST~5D tags in the admin DCC chat the same way they are coloured in the debug channel~2C using your theme. Turn off if your client shows the colour codes as junk.
  hadd dccore.swm k.WEBUI_HOST 2437 str 1 2436
  hadd dccore.swm pg.WEBUI_HOST 20
  hadd dccore.swm n.WEBUI_HOST Host
  hadd dccore.swm h.WEBUI_HOST Which addresses the dashboard listens on. 127.0.0.1 means only this computer can open it. 0.0.0.0 makes it reachable from other devices on your home network - never forward it to the internet~2C the connection is not encrypted.
  hadd dccore.swm k.WEBUI_PORT 2441 int 1 2440
  hadd dccore.swm pg.WEBUI_PORT 20
  hadd dccore.swm n.WEBUI_PORT Port
  hadd dccore.swm h.WEBUI_PORT The dashboard's port. Open http://127.0.0.1:8420 (or whatever you set) in your browser.
  hadd dccore.swm k.WEBUI_ALLOWED_HOSTS 2445 list 1 2444
  hadd dccore.swm pg.WEBUI_ALLOWED_HOSTS 20
  hadd dccore.swm n.WEBUI_ALLOWED_HOSTS Other names the dashboard answers to
  hadd dccore.swm h.WEBUI_ALLOWED_HOSTS Other names you open the dashboard by~2C separated by commas: a router's name for this computer~2C a reverse proxy~2C a VPN name. An IP address~2C localhost and this computer's own name always work; other names are refused. A single * turns the check off.
  hadd dccore.swm k.WEBUI_CONSOLE_ENABLED 2449 tri 1 2448
  hadd dccore.swm pg.WEBUI_CONSOLE_ENABLED 20
  hadd dccore.swm n.WEBUI_CONSOLE_ENABLED Enable the Console page (remote admin)
  hadd dccore.swm h.WEBUI_CONSOLE_ENABLED Show the Console page in the dashboard~2C which can run admin commands. Unset means on while the dashboard is reachable only from this computer~2C off otherwise - because on a network the dashboard is protected by the password alone.
  hadd dccore.swm k.WEBUI_OPEN_BROWSER 2453 bool 1 2453
  hadd dccore.swm pg.WEBUI_OPEN_BROWSER 20
  hadd dccore.swm n.WEBUI_OPEN_BROWSER Open the dashboard in a browser at startup
  hadd dccore.swm h.WEBUI_OPEN_BROWSER Open the dashboard in your browser automatically when the bot starts. Only happens when the dashboard is limited to this computer.
  hadd dccore.swm k.WEBUI_FOLDER_BROWSER_ENABLED 2457 bool 1 2457
  hadd dccore.swm pg.WEBUI_FOLDER_BROWSER_ENABLED 20
  hadd dccore.swm n.WEBUI_FOLDER_BROWSER_ENABLED Folder picker on the Settings page
  hadd dccore.swm h.WEBUI_FOLDER_BROWSER_ENABLED Show a folder picker on the Library page instead of typing paths. Off by default because it lets a logged-in dashboard user see the names of folders on this machine.
  hadd dccore.swm k.CONSOLE_SHOW_REQUESTS 2461 bool 1 2461
  hadd dccore.swm pg.CONSOLE_SHOW_REQUESTS 21
  hadd dccore.swm n.CONSOLE_SHOW_REQUESTS Show requests (who asked for what)
  hadd dccore.swm h.CONSOLE_SHOW_REQUESTS Show in the console who asked for which file.
  hadd dccore.swm k.CONSOLE_SHOW_QUEUE 2465 bool 1 2465
  hadd dccore.swm pg.CONSOLE_SHOW_QUEUE 21
  hadd dccore.swm n.CONSOLE_SHOW_QUEUE Show queue positions
  hadd dccore.swm h.CONSOLE_SHOW_QUEUE Show in the console when a request goes into somebody's queue~2C and at which position.
  hadd dccore.swm k.CONSOLE_SHOW_SENDS 2469 bool 1 2469
  hadd dccore.swm pg.CONSOLE_SHOW_SENDS 21
  hadd dccore.swm n.CONSOLE_SHOW_SENDS Show transfers starting~2C resuming and completing
  hadd dccore.swm h.CONSOLE_SHOW_SENDS Show in the console when a transfer starts~2C resumes and completes.
  hadd dccore.swm k.CONSOLE_SHOW_FAILURES 2473 bool 1 2473
  hadd dccore.swm pg.CONSOLE_SHOW_FAILURES 21
  hadd dccore.swm n.CONSOLE_SHOW_FAILURES Show failed transfers
  hadd dccore.swm h.CONSOLE_SHOW_FAILURES Show in the console when a transfer fails~2C and why.
  hadd dccore.swm k.CONSOLE_SHOW_SEARCHES 2477 bool 1 2477
  hadd dccore.swm pg.CONSOLE_SHOW_SEARCHES 21
  hadd dccore.swm n.CONSOLE_SHOW_SEARCHES Show searches and their result counts
  hadd dccore.swm h.CONSOLE_SHOW_SEARCHES Show in the console who searched for what~2C and how many results they got.
  hadd dccore.swm k.DEBUG_CHANNEL_FEED 2481 bool 1 2481
  hadd dccore.swm pg.DEBUG_CHANNEL_FEED 21
  hadd dccore.swm n.DEBUG_CHANNEL_FEED Also send requests~2C queue positions~2C starts and searches to the IRC debug channel
  hadd dccore.swm h.DEBUG_CHANNEL_FEED Also send requests~2C queue positions~2C transfer starts and searches to your IRC debug channel. Off by default: every line to a channel takes a turn in the same queue as the adverts and replies people are waiting for. Finished and failed transfers go there regardless.
  hadd dccore.swm k.DEBUG_MODE 2485 bool 1 2485
  hadd dccore.swm pg.DEBUG_MODE 22
  hadd dccore.swm n.DEBUG_MODE Debug mode
  hadd dccore.swm h.DEBUG_MODE Print every raw line the bot sends to the server in its own window. Very noisy - only for tracking down a connection problem.
  hadd dccore.swm k.DEBUG_TO_CHANNEL 2489 bool 1 2489
  hadd dccore.swm pg.DEBUG_TO_CHANNEL 22
  hadd dccore.swm n.DEBUG_TO_CHANNEL Send debug lines to channel
  hadd dccore.swm h.DEBUG_TO_CHANNEL Send the bot's running report (transfers~2C joins~2C bans~2C problems) to your debug channel.
  hadd dccore.swm k.DEBUG_TO_CONSOLE 2493 bool 1 2493
  hadd dccore.swm pg.DEBUG_TO_CONSOLE 22
  hadd dccore.swm n.DEBUG_TO_CONSOLE Send debug lines to admin console
  hadd dccore.swm h.DEBUG_TO_CONSOLE Send that same report to the admin console and the dashboard's Console page.
  hadd dccore.swm k.CONSOLE_TIMESTAMP_FORMAT 2497 str 1 2496
  hadd dccore.swm pg.CONSOLE_TIMESTAMP_FORMAT 22
  hadd dccore.swm n.CONSOLE_TIMESTAMP_FORMAT Time prefix on every console line (strftime; blank = none)
  hadd dccore.swm h.CONSOLE_TIMESTAMP_FORMAT The time shown at the start of every line in the bot's window. ~25H:~25M:~25S is hours:minutes:seconds; use ~25Y-~25m-~25d ~25H:~25M:~25S to include the date; leave blank for no time.
  hadd dccore.swm k.CONSOLE_LOG_FILE 2501 str 1 2500
  hadd dccore.swm pg.CONSOLE_LOG_FILE 22
  hadd dccore.swm n.CONSOLE_LOG_FILE Log file (blank = none)
  hadd dccore.swm h.CONSOLE_LOG_FILE Everything the bot's window shows is also saved here~2C with the date on every line~2C so it is still there after the window is closed. Leave blank for no log file.
  hadd dccore.swm k.CONSOLE_LOG_MAX_MB 2505 int 1 2504
  hadd dccore.swm pg.CONSOLE_LOG_MAX_MB 22
  hadd dccore.swm n.CONSOLE_LOG_MAX_MB Start a new log file at (MB)
  hadd dccore.swm h.CONSOLE_LOG_MAX_MB When the log file reaches this size it is renamed dccore.log.1 and a new one is started.
  hadd dccore.swm k.CONSOLE_LOG_KEEP 2509 int 1 2508
  hadd dccore.swm pg.CONSOLE_LOG_KEEP 22
  hadd dccore.swm n.CONSOLE_LOG_KEEP Old log files to keep
  hadd dccore.swm h.CONSOLE_LOG_KEEP How many of those older log files are kept before the oldest is deleted.
  hadd dccore.swm k.BOT_WINDOW 2513 choice 1 2512
  hadd dccore.swm pg.BOT_WINDOW 22
  hadd dccore.swm n.BOT_WINDOW The bot's window on Windows (normal~2C minimised~2C hidden)
  hadd dccore.swm ch.BOT_WINDOW normal minimised hidden
  hadd dccore.swm cl.BOT_WINDOW.1 normal
  hadd dccore.swm cl.BOT_WINDOW.2 minimised
  hadd dccore.swm cl.BOT_WINDOW.3 hidden
  hadd dccore.swm h.BOT_WINDOW How the bot runs on Windows when start-dccore.bat starts it: in its window (normal)~2C minimised to the taskbar~2C or with no window (hidden)~2C its output then in the log file.
  hadd dccore.swm k.PROJECT_URL 2517 str 1 2516
  hadd dccore.swm pg.PROJECT_URL 22
  hadd dccore.swm n.PROJECT_URL Project URL
  hadd dccore.swm h.PROJECT_URL Where DCCore comes from. Shown at the top of your list and in the reply to a version request.
  hadd dccore.swm k.TMP_ZIP_DIR 2521 str 1 2520
  hadd dccore.swm pg.TMP_ZIP_DIR 23
  hadd dccore.swm n.TMP_ZIP_DIR Temp archive directory
  hadd dccore.swm h.TMP_ZIP_DIR Where packed folders and list archives are built before sending. Cleaned up after each transfer.
  hadd dccore.swm k.LOCAL_LIST_DIR 2525 str 1 2524
  hadd dccore.swm pg.LOCAL_LIST_DIR 23
  hadd dccore.swm n.LOCAL_LIST_DIR Master list directory
  hadd dccore.swm h.LOCAL_LIST_DIR Where your published list files are kept.
  hadd dccore.swm k.FETCHED_FILES_DIR 2529 str 1 2528
  hadd dccore.swm pg.FETCHED_FILES_DIR 23
  hadd dccore.swm n.FETCHED_FILES_DIR Fetched files directory
  hadd dccore.swm h.FETCHED_FILES_DIR Where files you download from other bots are saved. Kept separate from your shared folders so they are not offered to others.
  hadd dccore.swm k.LIBRARY_FOLDERS_FILE 2533 str 1 2532
  hadd dccore.swm pg.LIBRARY_FOLDERS_FILE 23
  hadd dccore.swm n.LIBRARY_FOLDERS_FILE Served folders file
  hadd dccore.swm h.LIBRARY_FOLDERS_FILE Where the folders you added on the Library page are saved.
  hadd dccore.swm k.LISTS_FILE 2537 str 1 2536
  hadd dccore.swm pg.LISTS_FILE 23
  hadd dccore.swm n.LISTS_FILE Served lists file
  hadd dccore.swm h.LISTS_FILE Where your list definitions are saved~2C if you serve more than one list.
  hadd dccore.swm k.BANS_FILE 2541 str 1 2540
  hadd dccore.swm pg.BANS_FILE 23
  hadd dccore.swm n.BANS_FILE Bans file
  hadd dccore.swm h.BANS_FILE Where timed bans are saved.
  hadd dccore.swm k.HARD_BANS_FILE 2545 str 1 2544
  hadd dccore.swm pg.HARD_BANS_FILE 23
  hadd dccore.swm n.HARD_BANS_FILE Hard bans file
  hadd dccore.swm h.HARD_BANS_FILE Where permanent bans (from !ban) are saved.
  hadd dccore.swm k.STATS_FILE 2549 str 1 2548
  hadd dccore.swm pg.STATS_FILE 23
  hadd dccore.swm n.STATS_FILE Stats file
  hadd dccore.swm h.STATS_FILE Where the lifetime totals~2C the speed record and the daily figures are saved.
  hadd dccore.swm k.KNOWN_BOTS_FILE 2553 str 1 2552
  hadd dccore.swm pg.KNOWN_BOTS_FILE 23
  hadd dccore.swm n.KNOWN_BOTS_FILE Known bots file
  hadd dccore.swm h.KNOWN_BOTS_FILE Where the bot remembers the other bots it has seen advertising.
  hadd dccore.swm k.FETCHED_BOT_LISTS_FILE 2557 str 1 2556
  hadd dccore.swm pg.FETCHED_BOT_LISTS_FILE 23
  hadd dccore.swm n.FETCHED_BOT_LISTS_FILE Fetched bot lists file
  hadd dccore.swm h.FETCHED_BOT_LISTS_FILE Where the bot remembers which other bots' lists it holds.
  hadd dccore.swm k.LIST_INDEX_FILE 2561 str 1 2560
  hadd dccore.swm pg.LIST_INDEX_FILE 23
  hadd dccore.swm n.LIST_INDEX_FILE Cross-list search index
  hadd dccore.swm h.LIST_INDEX_FILE The search index over every list you have fetched from other bots. Can be large. Safe to delete with the bot stopped: the next start builds it again from the lists on disk~2C which takes a while with big lists.
  hadd dccore.swm k.LIST_AUDIO_INFO_CACHE 2565 str 1 2564
  hadd dccore.swm pg.LIST_AUDIO_INFO_CACHE 23
  hadd dccore.swm n.LIST_AUDIO_INFO_CACHE Audio info cache
  hadd dccore.swm h.LIST_AUDIO_INFO_CACHE Where the length and quality read from your audio files are kept between rebuilds. Safe to delete; the next rebuild reads every file again.
  hadd dccore.swm k.FETCH_HISTORY_FILE 2569 str 1 2568
  hadd dccore.swm pg.FETCH_HISTORY_FILE 23
  hadd dccore.swm n.FETCH_HISTORY_FILE Fetch history file
  hadd dccore.swm h.FETCH_HISTORY_FILE Where finished downloads from other bots are recorded for the Downloads page.
  hadd dccore.swm k.DOWNLOAD_COUNTS_FILE 2573 str 1 2572
  hadd dccore.swm pg.DOWNLOAD_COUNTS_FILE 23
  hadd dccore.swm n.DOWNLOAD_COUNTS_FILE Download counts file
  hadd dccore.swm h.DOWNLOAD_COUNTS_FILE Where the count of how often each file was sent is kept~2C for the Most downloaded table: a database beside it ending in .db.
  hadd dccore.swm k.TRANSFER_LOG_FILE 2577 str 1 2576
  hadd dccore.swm pg.TRANSFER_LOG_FILE 23
  hadd dccore.swm n.TRANSFER_LOG_FILE Transfer record file
  hadd dccore.swm h.TRANSFER_LOG_FILE Where a record of every transfer that ends - completed~2C failed or cancelled - is kept: what it was~2C its size~2C its speed~2C how long it waited in the queue~2C and the nick it went to or came from (no host~2C no channel). Shown on the Stats page~2C where a nick can be forgotten.
  hadd dccore.swm k.LIST_SIZE_FILE 2581 str 1 2580
  hadd dccore.swm pg.LIST_SIZE_FILE 23
  hadd dccore.swm n.LIST_SIZE_FILE List size file
  hadd dccore.swm h.LIST_SIZE_FILE The name of the small file written beside your list holding the library's total size~2C as shown in the advert.
  hadd dccore.swm k.LIST_RAWBYTES_FILE 2585 str 1 2584
  hadd dccore.swm pg.LIST_RAWBYTES_FILE 23
  hadd dccore.swm n.LIST_RAWBYTES_FILE List raw bytes file
  hadd dccore.swm h.LIST_RAWBYTES_FILE The name of the small file beside your list holding the exact byte total.
  hadd dccore.swm k.LIST_PROGRESS_FILE 2589 str 1 2588
  hadd dccore.swm pg.LIST_PROGRESS_FILE 23
  hadd dccore.swm n.LIST_PROGRESS_FILE List rebuild progress file
  hadd dccore.swm h.LIST_PROGRESS_FILE Where a running list rebuild reports its progress for the dashboard.
  hadd dccore.swm k.ADMIN_TOKENS_FILE 2593 str 1 2592
  hadd dccore.swm pg.ADMIN_TOKENS_FILE 23
  hadd dccore.swm n.ADMIN_TOKENS_FILE Paired console scripts file
  hadd dccore.swm h.ADMIN_TOKENS_FILE Where the login tokens of scripts paired with the admin console are kept (hashed~2C like the password). Made by the console's pair command; remove one with unpair.
  hadd dccore.swm k.ON_CONNECT_FILE 2597 str 1 2596
  hadd dccore.swm pg.ON_CONNECT_FILE 23
  hadd dccore.swm n.ON_CONNECT_FILE On-connect commands file
  hadd dccore.swm h.ON_CONNECT_FILE Where the commands sent on connect (such as your X login) are saved.
  hadd dccore.swm k.NOTICES_FILE 2601 str 1 2600
  hadd dccore.swm pg.NOTICES_FILE 23
  hadd dccore.swm n.NOTICES_FILE Operator notices file
  hadd dccore.swm h.NOTICES_FILE Where the notices shown on the dashboard - a lost connection~2C a kick or a refused join~2C a failed list rebuild - are saved.
  hadd dccore.swm k.PRIVATE_MESSAGES_FILE 2605 str 1 2604
  hadd dccore.swm pg.PRIVATE_MESSAGES_FILE 23
  hadd dccore.swm n.PRIVATE_MESSAGES_FILE Private messages file
  hadd dccore.swm h.PRIVATE_MESSAGES_FILE Where private messages to the bot are saved.
  hadd dccore.swm k.DCC_QUEUE_FILE 2609 str 1 2608
  hadd dccore.swm pg.DCC_QUEUE_FILE 23
  hadd dccore.swm n.DCC_QUEUE_FILE DCC queue file
  hadd dccore.swm h.DCC_QUEUE_FILE Where the per-user send queue is saved. The single-instance lock lives beside it.
  hadd dccore.swm k.CHAT_HISTORY_DAYS 2641 int 1 2640
  hadd dccore.swm pg.CHAT_HISTORY_DAYS 27
  hadd dccore.swm n.CHAT_HISTORY_DAYS Keep for (days)
  hadd dccore.swm h.CHAT_HISTORY_DAYS How many days dccore.mrc keeps DCCore Chat's history on your PC: whole months are deleted once they ended more than this many days ago. 0 keeps everything. The bot sends this to the script; a script that never had it deletes nothing.
  hadd dccore.swm k.CHAT_HISTORY_MAX_MB 2645 int 1 2644
  hadd dccore.swm pg.CHAT_HISTORY_MAX_MB 27
  hadd dccore.swm n.CHAT_HISTORY_MAX_MB At most (MB)
  hadd dccore.swm h.CHAT_HISTORY_MAX_MB How big DCCore Chat's history folder on your PC may grow~2C in MB: past it~2C the oldest months are deleted first~2C never the month running. 0 is no limit.
  hadd dccore.swm i.2000 SERVER
  hadd dccore.swm i.2001 SERVER
  hadd dccore.swm i.2004 PORT
  hadd dccore.swm i.2005 PORT
  hadd dccore.swm i.2008 NICKNAME
  hadd dccore.swm i.2009 NICKNAME
  hadd dccore.swm i.2012 ALT_NICKNAME
  hadd dccore.swm i.2013 ALT_NICKNAME
  hadd dccore.swm i.2016 REJOIN_ATTEMPTS
  hadd dccore.swm i.2017 REJOIN_ATTEMPTS
  hadd dccore.swm i.2020 ON_CONNECT_CHECK_MINUTES
  hadd dccore.swm i.2021 ON_CONNECT_CHECK_MINUTES
  hadd dccore.swm i.2025 @start.main
  hadd dccore.swm i.2029 @start.chat
  hadd dccore.swm i.2033 @start.downloads
  hadd dccore.swm i.2037 @auto
  hadd dccore.swm i.2041 SEARCH_ENABLED
  hadd dccore.swm i.2045 ANNOUNCE_TRANSFERS
  hadd dccore.swm i.2049 PRIVATE_MESSAGES_ENABLED
  hadd dccore.swm i.2053 WEBUI_ENABLED
  hadd dccore.swm i.2057 CTCP_VERSION_REPLY
  hadd dccore.swm i.2061 CHECK_FOR_UPDATES
  hadd dccore.swm i.2064 DEBUG_CHANNEL
  hadd dccore.swm i.2065 DEBUG_CHANNEL
  hadd dccore.swm i.1520 CHANNEL
  hadd dccore.swm i.2068 ADMIN_NICK
  hadd dccore.swm i.2069 ADMIN_NICK
  hadd dccore.swm i.2072 THEME
  hadd dccore.swm i.2073 THEME
  hadd dccore.swm i.2076 CUSTOM_THEME_BORDER
  hadd dccore.swm i.2077 CUSTOM_THEME_BORDER
  hadd dccore.swm i.2078 CUSTOM_THEME_BORDER
  hadd dccore.swm i.2080 CUSTOM_THEME_SEPARATOR
  hadd dccore.swm i.2081 CUSTOM_THEME_SEPARATOR
  hadd dccore.swm i.2082 CUSTOM_THEME_SEPARATOR
  hadd dccore.swm i.2084 CUSTOM_THEME_TEXTBOX
  hadd dccore.swm i.2085 CUSTOM_THEME_TEXTBOX
  hadd dccore.swm i.2086 CUSTOM_THEME_TEXTBOX
  hadd dccore.swm i.2088 CUSTOM_THEME_VALUE
  hadd dccore.swm i.2089 CUSTOM_THEME_VALUE
  hadd dccore.swm i.2090 CUSTOM_THEME_VALUE
  hadd dccore.swm i.2092 CUSTOM_THEME_ALERT
  hadd dccore.swm i.2093 CUSTOM_THEME_ALERT
  hadd dccore.swm i.2094 CUSTOM_THEME_ALERT
  hadd dccore.swm i.2096 CUSTOM_THEME_ACCENT
  hadd dccore.swm i.2097 CUSTOM_THEME_ACCENT
  hadd dccore.swm i.2098 CUSTOM_THEME_ACCENT
  hadd dccore.swm i.2100 ANNOUNCE_INTERVAL
  hadd dccore.swm i.2101 ANNOUNCE_INTERVAL
  hadd dccore.swm i.2104 BROADCAST_SEARCH_CHANNEL
  hadd dccore.swm i.2105 BROADCAST_SEARCH_CHANNEL
  hadd dccore.swm i.2108 BROADCAST_SEARCH_COOLDOWN
  hadd dccore.swm i.2109 BROADCAST_SEARCH_COOLDOWN
  hadd dccore.swm i.2112 MSG_DELAY
  hadd dccore.swm i.2113 MSG_DELAY
  hadd dccore.swm i.2116 DEBUG_MSG_DELAY
  hadd dccore.swm i.2117 DEBUG_MSG_DELAY
  hadd dccore.swm i.2120 LIST_BASE_NAME
  hadd dccore.swm i.2121 LIST_BASE_NAME
  hadd dccore.swm i.2124 LIST_FORMAT
  hadd dccore.swm i.2125 LIST_FORMAT
  hadd dccore.swm i.2128 LIST_IGNORED_EXTENSIONS
  hadd dccore.swm i.2129 LIST_IGNORED_EXTENSIONS
  hadd dccore.swm i.2133 SEPARATE_VIDEO_LIST
  hadd dccore.swm i.2136 LIST_VIDEO_EXTENSIONS
  hadd dccore.swm i.2137 LIST_VIDEO_EXTENSIONS
  hadd dccore.swm i.2140 LIST_VIDEO_COMPANION_EXTENSIONS
  hadd dccore.swm i.2141 LIST_VIDEO_COMPANION_EXTENSIONS
  hadd dccore.swm i.2145 RAR_ENABLED
  hadd dccore.swm i.2148 RAR_EXTENSIONS
  hadd dccore.swm i.2149 RAR_EXTENSIONS
  hadd dccore.swm i.2152 RAR_BINARY
  hadd dccore.swm i.2153 RAR_BINARY
  hadd dccore.swm i.2154 RAR_BINARY
  hadd dccore.swm i.2156 MAX_RAR_FOLDER_SIZE
  hadd dccore.swm i.2157 MAX_RAR_FOLDER_SIZE
  hadd dccore.swm i.2160 RAR_TIMEOUT
  hadd dccore.swm i.2161 RAR_TIMEOUT
  hadd dccore.swm i.2164 LIST_HEADER_FILE
  hadd dccore.swm i.2165 LIST_HEADER_FILE
  hadd dccore.swm i.2166 LIST_HEADER_FILE
  hadd dccore.swm i.2168 LIST_HEADER_MAX_BYTES
  hadd dccore.swm i.2169 LIST_HEADER_MAX_BYTES
  hadd dccore.swm i.2173 LIST_SHOW_AUDIO_INFO
  hadd dccore.swm i.2176 LIST_REBUILD_SCHEDULE
  hadd dccore.swm i.2177 LIST_REBUILD_SCHEDULE
  hadd dccore.swm i.2180 LIST_UPDATE_TIMEOUT
  hadd dccore.swm i.2181 LIST_UPDATE_TIMEOUT
  hadd dccore.swm i.2184 LIST_UPDATE_STALL_SECONDS
  hadd dccore.swm i.2185 LIST_UPDATE_STALL_SECONDS
  hadd dccore.swm i.2188 LIST_AUDIO_INFO_MINUTES
  hadd dccore.swm i.2189 LIST_AUDIO_INFO_MINUTES
  hadd dccore.swm i.2192 LIST_AUDIO_INFO_THREADS
  hadd dccore.swm i.2193 LIST_AUDIO_INFO_THREADS
  hadd dccore.swm i.2196 LIST_SCAN_THREADS
  hadd dccore.swm i.2197 LIST_SCAN_THREADS
  hadd dccore.swm i.2200 MAX_UPLOAD_KB
  hadd dccore.swm i.2201 MAX_UPLOAD_KB
  hadd dccore.swm i.2204 MAX_DOWNLOAD_KB
  hadd dccore.swm i.2205 MAX_DOWNLOAD_KB
  hadd dccore.swm i.2208 DCC_BLOCK_SIZE
  hadd dccore.swm i.2209 DCC_BLOCK_SIZE
  hadd dccore.swm i.2212 DCC_SEND_BUFFER
  hadd dccore.swm i.2213 DCC_SEND_BUFFER
  hadd dccore.swm i.2216 DCC_PORT_START
  hadd dccore.swm i.2217 DCC_PORT_START
  hadd dccore.swm i.2220 DCC_PORT_END
  hadd dccore.swm i.2221 DCC_PORT_END
  hadd dccore.swm i.2224 DCC_ACCEPT_TIMEOUT
  hadd dccore.swm i.2225 DCC_ACCEPT_TIMEOUT
  hadd dccore.swm i.2228 MAX_SEND_FAILS
  hadd dccore.swm i.2229 MAX_SEND_FAILS
  hadd dccore.swm i.2232 DAILY_SEND_FAILS_LIMIT
  hadd dccore.swm i.2233 DAILY_SEND_FAILS_LIMIT
  hadd dccore.swm i.2236 MAX_DCC_SLOTS
  hadd dccore.swm i.2237 MAX_DCC_SLOTS
  hadd dccore.swm i.2240 LARGE_FILE_MB
  hadd dccore.swm i.2241 LARGE_FILE_MB
  hadd dccore.swm i.2244 SLOW_SEND_MIN_KB
  hadd dccore.swm i.2245 SLOW_SEND_MIN_KB
  hadd dccore.swm i.2248 MAX_USER_QUEUE
  hadd dccore.swm i.2249 MAX_USER_QUEUE
  hadd dccore.swm i.2252 MAX_GLOBAL_QUEUE
  hadd dccore.swm i.2253 MAX_GLOBAL_QUEUE
  hadd dccore.swm i.2256 MAX_SEARCH_RESULTS
  hadd dccore.swm i.2257 MAX_SEARCH_RESULTS
  hadd dccore.swm i.2261 SEARCH_SHOW_FOLDER
  hadd dccore.swm i.2264 SEARCH_FOLDER_MAX_CHARS
  hadd dccore.swm i.2265 SEARCH_FOLDER_MAX_CHARS
  hadd dccore.swm i.2269 PAUSE_ON_UPDATE
  hadd dccore.swm i.2273 PAUSE_FOR_WHOLE_UPDATE
  hadd dccore.swm i.2276 REHASH_TRANSFER_WAIT
  hadd dccore.swm i.2277 REHASH_TRANSFER_WAIT
  hadd dccore.swm i.2281 AUTO_GRAB_LISTS
  hadd dccore.swm i.2284 AUTO_GRAB_EVERY_MINUTES
  hadd dccore.swm i.2285 AUTO_GRAB_EVERY_MINUTES
  hadd dccore.swm i.2288 AUTO_GRAB_MIN_FILES
  hadd dccore.swm i.2289 AUTO_GRAB_MIN_FILES
  hadd dccore.swm i.2292 AUTO_GRAB_MIN_SPEED_KB
  hadd dccore.swm i.2293 AUTO_GRAB_MIN_SPEED_KB
  hadd dccore.swm i.2297 AUTO_DISCOVER_CHANNEL_LISTS
  hadd dccore.swm i.2300 MULTI_CHANNEL_LIST_STABLE_SECONDS
  hadd dccore.swm i.2301 MULTI_CHANNEL_LIST_STABLE_SECONDS
  hadd dccore.swm i.2304 MAX_FETCH_SLOTS
  hadd dccore.swm i.2305 MAX_FETCH_SLOTS
  hadd dccore.swm i.2309 AUTO_REFETCH_LISTS
  hadd dccore.swm i.2312 AUTO_REFETCH_INTERVAL_HOURS
  hadd dccore.swm i.2313 AUTO_REFETCH_INTERVAL_HOURS
  hadd dccore.swm i.2316 AUTO_REFETCH_MAX_PER_RUN
  hadd dccore.swm i.2317 AUTO_REFETCH_MAX_PER_RUN
  hadd dccore.swm i.2320 FETCH_OFFER_TIMEOUT
  hadd dccore.swm i.2321 FETCH_OFFER_TIMEOUT
  hadd dccore.swm i.2324 FETCH_TRANSFER_TIMEOUT
  hadd dccore.swm i.2325 FETCH_TRANSFER_TIMEOUT
  hadd dccore.swm i.2328 FETCH_FOLDER_OFFER_TIMEOUT
  hadd dccore.swm i.2329 FETCH_FOLDER_OFFER_TIMEOUT
  hadd dccore.swm i.2332 FETCH_FOLDER_OFFER_TIMEOUT_UNADVERTISED
  hadd dccore.swm i.2333 FETCH_FOLDER_OFFER_TIMEOUT_UNADVERTISED
  hadd dccore.swm i.2336 FETCH_FOLDER_TRANSFER_TIMEOUT
  hadd dccore.swm i.2337 FETCH_FOLDER_TRANSFER_TIMEOUT
  hadd dccore.swm i.2340 MAX_FETCH_FILE_SIZE
  hadd dccore.swm i.2341 MAX_FETCH_FILE_SIZE
  hadd dccore.swm i.2344 MAX_FETCH_FOLDER_FILE_SIZE
  hadd dccore.swm i.2345 MAX_FETCH_FOLDER_FILE_SIZE
  hadd dccore.swm i.2348 MAX_FETCH_LIST_FILE_SIZE
  hadd dccore.swm i.2349 MAX_FETCH_LIST_FILE_SIZE
  hadd dccore.swm i.2352 MAX_LIST_TEXT_SIZE
  hadd dccore.swm i.2353 MAX_LIST_TEXT_SIZE
  hadd dccore.swm i.2356 FETCH_HISTORY_DAYS
  hadd dccore.swm i.2357 FETCH_HISTORY_DAYS
  hadd dccore.swm i.2360 FETCH_HISTORY_MAX_ROWS
  hadd dccore.swm i.2361 FETCH_HISTORY_MAX_ROWS
  hadd dccore.swm i.2364 FETCH_MAX_PER_BOT
  hadd dccore.swm i.2365 FETCH_MAX_PER_BOT
  hadd dccore.swm i.2368 FETCH_QUEUED_TIMEOUT
  hadd dccore.swm i.2369 FETCH_QUEUED_TIMEOUT
  hadd dccore.swm i.2372 FETCH_BOT_MAX_FAILS
  hadd dccore.swm i.2373 FETCH_BOT_MAX_FAILS
  hadd dccore.swm i.2376 FETCH_BOT_COOLDOWN_MINUTES
  hadd dccore.swm i.2377 FETCH_BOT_COOLDOWN_MINUTES
  hadd dccore.swm i.2380 LIST_REQUEST_TAG
  hadd dccore.swm i.2381 LIST_REQUEST_TAG
  hadd dccore.swm i.2384 MAX_REQUESTS
  hadd dccore.swm i.2385 MAX_REQUESTS
  hadd dccore.swm i.2388 REQUEST_WINDOW
  hadd dccore.swm i.2389 REQUEST_WINDOW
  hadd dccore.swm i.2392 MUTE_TIME
  hadd dccore.swm i.2393 MUTE_TIME
  hadd dccore.swm i.2396 FLOOD_BAN_SECONDS
  hadd dccore.swm i.2397 FLOOD_BAN_SECONDS
  hadd dccore.swm i.2400 PRIVATE_MESSAGE_COOLDOWN_SECONDS
  hadd dccore.swm i.2401 PRIVATE_MESSAGE_COOLDOWN_SECONDS
  hadd dccore.swm i.2404 PRIVATE_MESSAGE_DECLINE_TEXT
  hadd dccore.swm i.2405 PRIVATE_MESSAGE_DECLINE_TEXT
  hadd dccore.swm i.2408 PRIVATE_MESSAGE_DECLINE_INTERVAL_SECONDS
  hadd dccore.swm i.2409 PRIVATE_MESSAGE_DECLINE_INTERVAL_SECONDS
  hadd dccore.swm i.2412 PRIVATE_MESSAGE_DECLINE_BURST
  hadd dccore.swm i.2413 PRIVATE_MESSAGE_DECLINE_BURST
  hadd dccore.swm i.2416 PRIVATE_MESSAGE_DECLINE_BURST_SECONDS
  hadd dccore.swm i.2417 PRIVATE_MESSAGE_DECLINE_BURST_SECONDS
  hadd dccore.swm i.2420 ADMIN_HOSTMASKS
  hadd dccore.swm i.2421 ADMIN_HOSTMASKS
  hadd dccore.swm i.2424 ADMIN_CHAT_MODE
  hadd dccore.swm i.2425 ADMIN_CHAT_MODE
  hadd dccore.swm i.2429 ADMIN_CHANNEL_COMMANDS
  hadd dccore.swm i.2433 ADMIN_CHAT_COLOURS
  hadd dccore.swm i.2436 WEBUI_HOST
  hadd dccore.swm i.2437 WEBUI_HOST
  hadd dccore.swm i.2440 WEBUI_PORT
  hadd dccore.swm i.2441 WEBUI_PORT
  hadd dccore.swm i.2444 WEBUI_ALLOWED_HOSTS
  hadd dccore.swm i.2445 WEBUI_ALLOWED_HOSTS
  hadd dccore.swm i.2448 WEBUI_CONSOLE_ENABLED
  hadd dccore.swm i.2449 WEBUI_CONSOLE_ENABLED
  hadd dccore.swm i.2453 WEBUI_OPEN_BROWSER
  hadd dccore.swm i.2457 WEBUI_FOLDER_BROWSER_ENABLED
  hadd dccore.swm i.2461 CONSOLE_SHOW_REQUESTS
  hadd dccore.swm i.2465 CONSOLE_SHOW_QUEUE
  hadd dccore.swm i.2469 CONSOLE_SHOW_SENDS
  hadd dccore.swm i.2473 CONSOLE_SHOW_FAILURES
  hadd dccore.swm i.2477 CONSOLE_SHOW_SEARCHES
  hadd dccore.swm i.2481 DEBUG_CHANNEL_FEED
  hadd dccore.swm i.2485 DEBUG_MODE
  hadd dccore.swm i.2489 DEBUG_TO_CHANNEL
  hadd dccore.swm i.2493 DEBUG_TO_CONSOLE
  hadd dccore.swm i.2496 CONSOLE_TIMESTAMP_FORMAT
  hadd dccore.swm i.2497 CONSOLE_TIMESTAMP_FORMAT
  hadd dccore.swm i.2500 CONSOLE_LOG_FILE
  hadd dccore.swm i.2501 CONSOLE_LOG_FILE
  hadd dccore.swm i.2502 CONSOLE_LOG_FILE
  hadd dccore.swm i.2504 CONSOLE_LOG_MAX_MB
  hadd dccore.swm i.2505 CONSOLE_LOG_MAX_MB
  hadd dccore.swm i.2508 CONSOLE_LOG_KEEP
  hadd dccore.swm i.2509 CONSOLE_LOG_KEEP
  hadd dccore.swm i.2512 BOT_WINDOW
  hadd dccore.swm i.2513 BOT_WINDOW
  hadd dccore.swm i.2516 PROJECT_URL
  hadd dccore.swm i.2517 PROJECT_URL
  hadd dccore.swm i.2520 TMP_ZIP_DIR
  hadd dccore.swm i.2521 TMP_ZIP_DIR
  hadd dccore.swm i.2522 TMP_ZIP_DIR
  hadd dccore.swm i.2524 LOCAL_LIST_DIR
  hadd dccore.swm i.2525 LOCAL_LIST_DIR
  hadd dccore.swm i.2526 LOCAL_LIST_DIR
  hadd dccore.swm i.2528 FETCHED_FILES_DIR
  hadd dccore.swm i.2529 FETCHED_FILES_DIR
  hadd dccore.swm i.2530 FETCHED_FILES_DIR
  hadd dccore.swm i.2532 LIBRARY_FOLDERS_FILE
  hadd dccore.swm i.2533 LIBRARY_FOLDERS_FILE
  hadd dccore.swm i.2534 LIBRARY_FOLDERS_FILE
  hadd dccore.swm i.2536 LISTS_FILE
  hadd dccore.swm i.2537 LISTS_FILE
  hadd dccore.swm i.2538 LISTS_FILE
  hadd dccore.swm i.2540 BANS_FILE
  hadd dccore.swm i.2541 BANS_FILE
  hadd dccore.swm i.2542 BANS_FILE
  hadd dccore.swm i.2544 HARD_BANS_FILE
  hadd dccore.swm i.2545 HARD_BANS_FILE
  hadd dccore.swm i.2546 HARD_BANS_FILE
  hadd dccore.swm i.2548 STATS_FILE
  hadd dccore.swm i.2549 STATS_FILE
  hadd dccore.swm i.2550 STATS_FILE
  hadd dccore.swm i.2552 KNOWN_BOTS_FILE
  hadd dccore.swm i.2553 KNOWN_BOTS_FILE
  hadd dccore.swm i.2554 KNOWN_BOTS_FILE
  hadd dccore.swm i.2556 FETCHED_BOT_LISTS_FILE
  hadd dccore.swm i.2557 FETCHED_BOT_LISTS_FILE
  hadd dccore.swm i.2558 FETCHED_BOT_LISTS_FILE
  hadd dccore.swm i.2560 LIST_INDEX_FILE
  hadd dccore.swm i.2561 LIST_INDEX_FILE
  hadd dccore.swm i.2562 LIST_INDEX_FILE
  hadd dccore.swm i.2564 LIST_AUDIO_INFO_CACHE
  hadd dccore.swm i.2565 LIST_AUDIO_INFO_CACHE
  hadd dccore.swm i.2566 LIST_AUDIO_INFO_CACHE
  hadd dccore.swm i.2568 FETCH_HISTORY_FILE
  hadd dccore.swm i.2569 FETCH_HISTORY_FILE
  hadd dccore.swm i.2570 FETCH_HISTORY_FILE
  hadd dccore.swm i.2572 DOWNLOAD_COUNTS_FILE
  hadd dccore.swm i.2573 DOWNLOAD_COUNTS_FILE
  hadd dccore.swm i.2574 DOWNLOAD_COUNTS_FILE
  hadd dccore.swm i.2576 TRANSFER_LOG_FILE
  hadd dccore.swm i.2577 TRANSFER_LOG_FILE
  hadd dccore.swm i.2578 TRANSFER_LOG_FILE
  hadd dccore.swm i.2580 LIST_SIZE_FILE
  hadd dccore.swm i.2581 LIST_SIZE_FILE
  hadd dccore.swm i.2582 LIST_SIZE_FILE
  hadd dccore.swm i.2584 LIST_RAWBYTES_FILE
  hadd dccore.swm i.2585 LIST_RAWBYTES_FILE
  hadd dccore.swm i.2586 LIST_RAWBYTES_FILE
  hadd dccore.swm i.2588 LIST_PROGRESS_FILE
  hadd dccore.swm i.2589 LIST_PROGRESS_FILE
  hadd dccore.swm i.2590 LIST_PROGRESS_FILE
  hadd dccore.swm i.2592 ADMIN_TOKENS_FILE
  hadd dccore.swm i.2593 ADMIN_TOKENS_FILE
  hadd dccore.swm i.2594 ADMIN_TOKENS_FILE
  hadd dccore.swm i.2596 ON_CONNECT_FILE
  hadd dccore.swm i.2597 ON_CONNECT_FILE
  hadd dccore.swm i.2598 ON_CONNECT_FILE
  hadd dccore.swm i.2600 NOTICES_FILE
  hadd dccore.swm i.2601 NOTICES_FILE
  hadd dccore.swm i.2602 NOTICES_FILE
  hadd dccore.swm i.2604 PRIVATE_MESSAGES_FILE
  hadd dccore.swm i.2605 PRIVATE_MESSAGES_FILE
  hadd dccore.swm i.2606 PRIVATE_MESSAGES_FILE
  hadd dccore.swm i.2608 DCC_QUEUE_FILE
  hadd dccore.swm i.2609 DCC_QUEUE_FILE
  hadd dccore.swm i.2610 DCC_QUEUE_FILE
  hadd dccore.swm i.2613 @panel
  hadd dccore.swm i.2617 @titlebar
  hadd dccore.swm i.2621 @separate
  hadd dccore.swm i.2625 @beep
  hadd dccore.swm i.2629 @nodcx
  hadd dccore.swm i.2633 @chat.all
  hadd dccore.swm i.2637 @chat.popup
  hadd dccore.swm i.2640 CHAT_HISTORY_DAYS
  hadd dccore.swm i.2641 CHAT_HISTORY_DAYS
  hadd dccore.swm i.2644 CHAT_HISTORY_MAX_MB
  hadd dccore.swm i.2645 CHAT_HISTORY_MAX_MB
  hadd dccore.swm lo.start.main 2025
  hadd dccore.swm h.@start.main This mIRC's own setting~2C kept in dccore.ini beside the script - not the bot's. Saved by Apply or OK.
  hadd dccore.swm lo.start.chat 2029
  hadd dccore.swm h.@start.chat This mIRC's own setting~2C kept in dccore.ini beside the script - not the bot's. Saved by Apply or OK.
  hadd dccore.swm lo.start.downloads 2033
  hadd dccore.swm h.@start.downloads This mIRC's own setting~2C kept in dccore.ini beside the script - not the bot's. Saved by Apply or OK.
  hadd dccore.swm lo.auto 2037
  hadd dccore.swm h.@auto This mIRC's own setting~2C kept in dccore.ini beside the script - not the bot's. Saved by Apply or OK.
  hadd dccore.swm lo.panel 2613
  hadd dccore.swm h.@panel This mIRC's own setting~2C kept in dccore.ini beside the script - not the bot's. Saved by Apply or OK.
  hadd dccore.swm lo.titlebar 2617
  hadd dccore.swm h.@titlebar This mIRC's own setting~2C kept in dccore.ini beside the script - not the bot's. Saved by Apply or OK.
  hadd dccore.swm lo.separate 2621
  hadd dccore.swm h.@separate This mIRC's own setting~2C kept in dccore.ini beside the script - not the bot's. Saved by Apply or OK.
  hadd dccore.swm lo.beep 2625
  hadd dccore.swm h.@beep This mIRC's own setting~2C kept in dccore.ini beside the script - not the bot's. Saved by Apply or OK.
  hadd dccore.swm lo.nodcx 2629
  hadd dccore.swm h.@nodcx This mIRC's own setting~2C kept in dccore.ini beside the script - not the bot's. Saved by Apply or OK.
  hadd dccore.swm lo.chat.all 2633
  hadd dccore.swm h.@chat.all This mIRC's own setting~2C kept in dccore.ini beside the script - not the bot's. Saved by Apply or OK.
  hadd dccore.swm lo.chat.popup 2637
  hadd dccore.swm h.@chat.popup This mIRC's own setting~2C kept in dccore.ini beside the script - not the bot's. Saved by Apply or OK.
  hadd dccore.swm locals start.main start.chat start.downloads auto panel titlebar separate beep nodcx chat.all chat.popup
  hadd dccore.swm br.2154 file RAR_BINARY
  hadd dccore.swm br.2166 file LIST_HEADER_FILE
  hadd dccore.swm br.2502 file CONSOLE_LOG_FILE
  hadd dccore.swm br.2522 dir TMP_ZIP_DIR
  hadd dccore.swm br.2526 dir LOCAL_LIST_DIR
  hadd dccore.swm br.2530 dir FETCHED_FILES_DIR
  hadd dccore.swm br.2534 file LIBRARY_FOLDERS_FILE
  hadd dccore.swm br.2538 file LISTS_FILE
  hadd dccore.swm br.2542 file BANS_FILE
  hadd dccore.swm br.2546 file HARD_BANS_FILE
  hadd dccore.swm br.2550 file STATS_FILE
  hadd dccore.swm br.2554 file KNOWN_BOTS_FILE
  hadd dccore.swm br.2558 file FETCHED_BOT_LISTS_FILE
  hadd dccore.swm br.2562 file LIST_INDEX_FILE
  hadd dccore.swm br.2566 file LIST_AUDIO_INFO_CACHE
  hadd dccore.swm br.2570 file FETCH_HISTORY_FILE
  hadd dccore.swm br.2574 file DOWNLOAD_COUNTS_FILE
  hadd dccore.swm br.2578 file TRANSFER_LOG_FILE
  hadd dccore.swm br.2582 file LIST_SIZE_FILE
  hadd dccore.swm br.2586 file LIST_RAWBYTES_FILE
  hadd dccore.swm br.2590 file LIST_PROGRESS_FILE
  hadd dccore.swm br.2594 file ADMIN_TOKENS_FILE
  hadd dccore.swm br.2598 file ON_CONNECT_FILE
  hadd dccore.swm br.2602 file NOTICES_FILE
  hadd dccore.swm br.2606 file PRIVATE_MESSAGES_FILE
  hadd dccore.swm br.2610 file DCC_QUEUE_FILE
  hadd dccore.swm bot.n 6
  hadd dccore.swm bot.1 2001,2005,2009,2013,2017,2021,1500,1501,1502,1503,1506,1505,2041,2045,2049,2053,2057,2061,2065,1520,1521,1522,1523,2069,2073,1610,1611,2077,2078,2081,2082,2085,2086,2089,2090,2093,2094,2097,2098,2101,2105,2109,2113,2117,1540,1551,1554,1555,1557,1700
  hadd dccore.swm bot.2 1701,1702,1703,1704,1705,1706,1707,1708,1709,1710,1711,1712,1713,1714,1715,1716,1717,1718,1719,1720,1721,1722,1723,1724,1725,1726,1727,1728,1729,1730,1731,1732,1733,1734,1735,1736,1737,1738,1739,1740,1741,1742,1743,1744,1745,1746,1747,1748,1749,1750
  hadd dccore.swm bot.3 1751,1752,1753,1754,1755,1756,1757,1758,1759,1774,1770,1771,1772,1773,1567,1568,2121,2125,2129,2133,2137,2141,2145,2149,2153,2154,2157,2161,2165,2166,2169,2173,2177,2181,2185,2189,2193,2197,2201,2205,2209,2213,2217,2221,2225,2229,2233,2237,2241,2245
  hadd dccore.swm bot.4 2249,2253,2257,2261,2265,2269,2273,2277,2281,2285,2289,2293,2297,2301,2305,2309,2313,2317,2321,2325,2329,2333,2337,2341,2345,2349,2353,2357,2361,2365,2369,2373,2377,2381,2385,2389,2393,2397,1580,1583,1584,1585,1586,1587,1581,1582,1595,1597,1598,1596
  hadd dccore.swm bot.5 2401,2405,2409,2413,2417,2421,2425,2429,2433,2437,2441,2445,2449,2453,2457,2461,2465,2469,2473,2477,2481,2485,2489,2493,2497,2501,2502,2505,2509,2513,2517,2521,2522,2525,2526,2529,2530,2533,2534,2537,2538,2541,2542,2545,2546,2549,2550,2553,2554,2557
  hadd dccore.swm bot.6 2558,2561,2562,2565,2566,2569,2570,2573,2574,2577,2578,2581,2582,2585,2586,2589,2590,2593,2594,2597,2598,2601,2602,2605,2606,2609,2610,2641,2645
  hadd dccore.swm confirm TMP_ZIP_DIR LOCAL_LIST_DIR FETCHED_FILES_DIR LIBRARY_FOLDERS_FILE LISTS_FILE BANS_FILE HARD_BANS_FILE STATS_FILE KNOWN_BOTS_FILE FETCHED_BOT_LISTS_FILE LIST_INDEX_FILE LIST_AUDIO_INFO_CACHE FETCH_HISTORY_FILE DOWNLOAD_COUNTS_FILE TRANSFER_LOG_FILE LIST_SIZE_FILE LIST_RAWBYTES_FILE LIST_PROGRESS_FILE ADMIN_TOKENS_FILE ON_CONNECT_FILE NOTICES_FILE PRIVATE_MESSAGES_FILE DCC_QUEUE_FILE
  hadd dccore.swm t.1500 Sent once the server has registered the bot~2C before it joins: one command per line~2C exactly as typed into a client. ~25nick~25 is the nickname the server gave it.
  hadd dccore.swm t.2404 That reply's wording (~25admin becomes the admin nick)
  hadd dccore.swm colours 00 white~2C01 black~2C02 blue~2C03 green~2C04 red~2C05 maroon~2C06 purple~2C07 orange~2C08 yellow~2C09 light green~2C10 cyan~2C11 light cyan~2C12 royal blue~2C13 pink~2C14 grey~2C15 light grey
  hadd dccore.swm themedefault Theme default
  hadd dccore.swm keepprevious Keep previous
  hadd dccore.swm tri Not set (automatic)~2COn~2COff
  hadd dccore.swm modes normal quiet request_only
  hadd dccore.swm modelabels Normal~2CQuiet (no advert)~2CRequest only (silent)
}
; ==== END GENERATED settings window ====


; ---------------------------------------------------------------------
;  DCCore Chat - public operator chat, relayed by the bot (#371)
; ---------------------------------------------------------------------
;
;  Operators chatting with each other in the channels their bots share.
;  The BOT is the relay: what you type in the chat window goes to the bot
;  over this console (`chat * text`), and the bot says it as an ordinary
;  channel message starting with a tag, in the fewest channels that reach
;  the other DCCore bots (a channel NOTICE is what channel bots kick for).
;  A tagged message from another DCCore bot - recognised by its realname,
;  which the bot looks up with WHO - comes back here as a CHAT line. Your
;  own mIRC does not have to be in any channel.
;
;  The DCCore bots WHO has found are listed to the right of the window
;  (plain nicknames). Double-click one, or right-click it and pick "Message
;  this bot privately", to send there instead - a private message to one
;  known peer (#371 follow-up), never a channel, and never to a nick the
;  bot has not itself found: that is the only proof it has that the other
;  end can see it.
;
;  It is PUBLIC, and says so. A channel message reaches everybody in it;
;  somebody without a chat window sees the line in the channel as it is.
;  The sender is the nick that sent it - your bot, for what you type - and
;  the tag is a presentation filter, not a trust boundary.
;
;  The bot does the checking - the tag as the first word, a sender that is
;  another DCCore bot, its own channels only, control codes out, a per-nick
;  flood limit, a cap on what you send - and keeps the last lines in
;  memory, so a window that reconnects shows what it missed. Each line
;  carries an id (its time in milliseconds), and the window never draws the
;  same one twice.
;
;  This script only ever sends what you typed. Nothing in the message
;  handler or in anything drawing a CHAT line sends anything, so two
;  scripts can never echo each other.
;
;  The tag is neutral so that a script that is not DCCore's can speak it too.
;  It cannot change once people use it.
;
;  History (#1393). The bot keeps nothing on disk; THIS script does. It is
;  ON by default - for an operator who only updated the script too - until
;  "Keep a history file" is unticked in the window's right-click menu, and
;  the window says where it keeps it when it opens. Every chat line the
;  window draws - private conversations included; not the bot's own
;  remarks, not the script's grey lines - goes into mIRC's LOG folder
;  ($logdir, which can be outside the mIRC folder), in DCCore-Chat\:
;    #channel\YYYY-MM.log  a folder per channel, a file per month, "@nick\"
;                        for a private conversation, every-channel\ for your
;                        own line said with `chat *` in several channels at
;                        once - "yyyy-mm-dd HH:nn:ss channel nick text",
;                        plain text; whole months are deleted by the
;                        history's limits (#1401, below)
;    recent.txt          the last lines of all of them together, as
;                        "<id> <channel> <nick> own|other <text>", cut back
;                        to the last 200 once it passes 400
;  When the window opens, the last 200 lines of recent.txt are drawn in grey,
;  under "Earlier", by the same rules as a live line: channels listened on,
;  private lines and your own. They are not written again and not counted
;  as unread, and chat.last moves past them, so the bot's own replay after
;  HELLO cannot draw any of them a second time - but never past the clock
;  (an id more than five minutes ahead of $ctime): one line from a bot whose
;  clock ran ahead, or from before the PC's clock was put back, would
;  otherwise hold chat.last above every line to come, and the window would
;  draw and keep nothing more. Such a line is still drawn. Turning the
;  history off stops the writing and the replay; the files stay, until the
;  limits remove them.
;
;  Limits (#1401). How long and how much is kept are the BOT's settings,
;  CHAT_HISTORY_DAYS and CHAT_HISTORY_MAX_MB - on its dashboard under
;  Settings -> Private messages, and on the control panel's mIRC side -
;  sent as DCCORE CHATHIST <days> <max_mb> after HELLO and when they change.
;  The last values are kept in dccore.ini, so they hold before the next
;  connection too; until a bot has sent them nothing is deleted, and 0 is
;  no limit. They delete whole month files: every month that ended more
;  than the days ago, then, while the folder is over the size, the oldest
;  months - never the month running. recent.txt is not touched.
;
;  Unread (#1393). A line from another bot drawn while the window is not
;  the active one - or while mIRC itself is in the background - counts in
;  the title ("3 unread") until you are back in the window. Its switchbar
;  button is lit as for any new line, and with "Flash on a new line" ticked
;  /flash makes mIRC flash in the taskbar - which it does only while mIRC
;  is in the background.

alias dccore.chat.tag { return $+($chr(91),ServersChat,$chr(93)) }
alias dccore.chat.win { return @DCCore-Chat }

; Is this channel one we listen on? Every channel the bot is in when "all
; channels" is ticked; otherwise only the ones ticked in the window's menu.
alias dccore.chat.listens {
  if ($dccore.opt(chat.all)) { return $true }
  return $istok($dccore.opt(chat.listen),$1,32)
}
; Is the relay up: connected to the bot, and its console in structured mode?
alias dccore.chat.relaying { return $iif(($dccore.st(state) == in) && ($dccore.st(mode) == structured),$true,$false) }

; The window: "DCCore Chat", public, saying where a typed line goes.
; $1 = quiet: opened by an arriving line, so minimised with its button
; lit rather than taking the focus from whatever you were typing in.
; -l16: a side-listbox 16 characters wide, for the DCCore bots WHO has
; found (#371 follow-up) - plain nicknames, nothing else drawn on them.
; $1 = start: opened when mIRC starts (#1201) - minimised too, with its
; button at the end of the switchbar.
alias dccore.chat.window {
  if ($window($dccore.chat.win)) { return }
  if ($1 == quiet) { window -enl16 $dccore.chat.win }
  elseif ($1 == start) { window -enzl16 $dccore.chat.win }
  else { window -el16 $dccore.chat.win }
  if ($dccore.opt(font)) { font $dccore.chat.win $dccore.fontsize Lucida Console }
  dccore.chat.title
  dccore.chat.sys Public: everyone in the channel reads what is typed here, with or without this script.
  if ($dccore.opt(chat.history)) { dccore.chat.sys What this window shows, private conversations too, is kept in $dccore.chat.hist.dir - right-click to stop keeping a history. }
  dccore.chat.sys What you type is said by $iif($dccore.bot,$dccore.bot,your bot) in the channels where it has seen other DCCore bots. Right-click to send to one channel instead, and to choose the ones to listen on.
  dccore.chat.sys Double-click a bot in the list on the right (or right-click it) to message it privately instead.
  dccore.chat.hist.replay
  dccore.chat.hist.daily
  dccore.chat.peers.redraw
}
alias dccore.chat.title {
  if (!$window($dccore.chat.win)) { return }
  var %to = $dccore.opt(chat.to)
  ; /var stores a condition as its TEXT - never empty, so always "true" to
  ; $iif - so each is evaluated by $iif here (#1041). And "privately" is
  ; decided by where the line goes, the automatic target included: with no
  ; pick, a peer who wrote privately is answered privately.
  var %auto = $iif((%to == $null) || (%to == *),1,0)
  var %target = $iif(%auto,$dccore.st(chat.replyto),%to)
  var %where = $iif(%target != $null,%target,every channel with other DCCore bots)
  var %priv = $iif((%target != $null) && ($left(%target,1) !isin $+($chr(35),&+!)),1,0)
  ; The lines from other bots drawn while the window was not active (#1393).
  titlebar $dccore.chat.win DCCore Chat $dccore.dot public $dccore.dot typing sends $iif(%priv,privately to,to) %where $iif(!$dccore.chat.relaying,$dccore.dot not connected to the bot) $iif($dccore.st(chat.unread) > 0,$dccore.dot $dccore.st(chat.unread) unread)
}
alias dccore.chat.sys {
  dccore.chat.window
  echo 14 -i2 $dccore.chat.win $1-
}
; A chat line's text as the window draws it: past 400 characters, the first
; 397 and "...". The live line and the replayed one both.
alias dccore.chat.cut {
  if ($len($1-) > 400) { return $left($1-,397) $+ ... }
  return $1-
}
; One chat line: $1 channel, $2 nick, $3 own or other, $4 time, $5- text.
alias dccore.chat.show {
  dccore.chat.window quiet
  var %text = $dccore.chat.cut($5-)
  var %who = $+($chr(3),$iif($3 == own,$dccore.col(console),$dccore.col(name)),$chr(2),$2,$chr(15))
  var %line = $+($chr(3),14,$4,$chr(160),$1,$chr(15)) %who %text
  if ($version >= 7) { echo -mi2 $dccore.chat.win %line }
  else { echo -i2 $dccore.chat.win %line }
}

; A CHAT line from the bot: <id> <channel> <nick> <text>. The nick "*" is
; the bot's own remark (somebody hidden for flooding).
alias dccore.chat.feed {
  if ($1 !isnum) { return }
  ; The first line of a session: the history file's newest id counts as
  ; drawn (#1393), so a line the bot replays after HELLO that the window will
  ; also replay from the file - when this line is what opens it - is drawn
  ; and written once.
  if ($dccore.st(chat.last) !isnum) { dccore.chat.hist.floor }
  if ($dccore.st(chat.last) isnum) && ($1 <= $dccore.st(chat.last)) { return }
  hadd dccore.live chat.last $1
  ; Somebody else's line names where the conversation is: typing with no
  ; channel manually picked replies there, not to wherever a set-cover
  ; happens to land (#958 follow-up). Never "-" or "*" - those are only an
  ; OWN fan-out line's channel. A private line's channel is "@<nick>" -
  ; reply there means privately to that nick, so the "@" is dropped.
  ;
  ; A PRIVATE CONVERSATION IS NEVER MOVED TO A CHANNEL BY ITSELF. Any line
  ; used to move the target, so one arriving from a channel while a private
  ; reply was being typed sent that reply to the channel, in public. Now a
  ; private line always takes the target, and a channel line only while the
  ; target is no one or a channel - and only from a channel listened on,
  ; since a line nobody sees is no reason to move. The bot's own remarks
  ; (nick "*") never move it. A manual pick still overrides all of this.
  if ($3 != $dccore.bot) && ($3 != *) && ($2 != $null) && ($2 != -) {
    var %rt = $dccore.st(chat.replyto)
    if ($left($2,1) == @) { hadd dccore.live chat.replyto $mid($2,2-) | dccore.chat.title }
    elseif ($dccore.chat.listens($2)) && ((%rt == $null) || ($left(%rt,1) isin $+($chr(35),&))) { hadd dccore.live chat.replyto $2 | dccore.chat.title }
  }
  ; Your own lines, and any private line (its "@<nick>" channel was never
  ; something to tick in the Listen on menu), always show (#958 follow-up):
  ; one said with `chat *` comes back with "-" for its channel, since it
  ; went to several.
  if ($3 != $dccore.bot) && ($left($2,1) != @) && (!$dccore.chat.listens($2)) { return }
  if (!$window($dccore.chat.win)) && (!$dccore.opt(chat.popup)) { return }
  if ($3 == *) { dccore.chat.sys $2 $strip($4-) | return }
  var %kind = $iif($3 == $dccore.bot,own,other)
  dccore.chat.show $iif($2 == -,*,$2) $3 %kind $asctime($int($calc($1 / 1000)),HH:nn) $strip($4-)
  ; Drawn: into the history file, after the window has replayed the file
  ; (so not twice), then counted as unread - last, as /flash is the one
  ; step here that touches something outside the script (#1393).
  dccore.chat.hist.add $1 $2 $3 %kind $strip($4-)
  if (%kind == other) { dccore.chat.unread.add }
}
; A CHANNELS line: the channels the bot is in, which are the ones it chats in.
alias dccore.chat.channels {
  if ($1- == $null) { hdel dccore.live chat.chans | return }
  hadd dccore.live chat.chans $1-
}

; A tagged message that reaches YOUR mIRC as well, because you are in that
; channel too: the bot relays it, so it is drawn from its CHAT line - and
; kept out of the channel view here, but ONLY while the relay is up, the
; bot is in that channel, and the line will be drawn. With the relay down
; nothing would draw it, so it is left to show in the channel as usual.
;
; #982 audit finding 1: checking the tag, relay state, channel and listen
; settings is not the same as knowing the bot will actually relay it - a
; person, a script that is not DCCore's (the tag is neutral on purpose),
; or a DCCore bot WHO has not found yet all say the same tagged text, and
; the bot's capture() drops every one of those (unknown sender, or empty
; after stripping) with nothing arriving to draw. Hiding it here anyway
; made the message vanish for the operator while everyone else in the
; channel still read it. Checked against the same peer list the
; side-listbox is drawn from, so only a line the bot is actually going to
; relay is ever hidden - a peer muted for flooding or over the all-senders
; cap in the few seconds after being confirmed known is the one case this
; cannot see coming, and is rare and short next to what it replaces.
on ^*:TEXT:*:#: {
  if ($1 != $dccore.chat.tag) { return }
  if ($2 == $null) { return }
  if (!$dccore.chat.relaying) { return }
  if (!$dccore.here) { return }
  if (!$istok($dccore.st(chat.chans),$chan,32)) { return }
  ; The bot's OWN relayed line is never in dccore.chatpeers - peers are
  ; other DCCore bots by definition (the Python side excludes its own nick
  ; from every PEERS line it ever sends, see serverschat.note_who_reply()).
  ; The #982 audit's peer check above missed this: it made a tagged line
  ; from an unknown sender correctly stay visible, but also un-hid every
  ; line the operator's own bot said - seen live, right after that fix
  ; shipped. $dccore.bot is checked first, same nick comparison the
  ; reconnect-detection hooks already use elsewhere in this file.
  if ($nick != $dccore.bot) && (!$istok($hget(dccore.chatpeers,$lower($chan)),$nick,32)) { return }
  if (!$dccore.chat.listens($chan)) { return }
  if (!$window($dccore.chat.win)) && (!$dccore.opt(chat.popup)) { return }
  haltdef
}

; Sending: typed in the window, or /dccore chat <text>. To the bot, which
; says it in the channel picked for it; the line comes back as a CHAT line.
alias dccore.chat.say {
  var %to = $dccore.opt(chat.to)
  ; No channel manually picked (right-click), or explicitly "every channel":
  ; reply where the conversation is - the channel (or peer) the last line
  ; other than your own arrived on - and only broadcast to reach everyone
  ; when nobody has said anything back yet (#958 follow-up).
  if (%to == $null) || (%to == *) { var %to = $iif($dccore.st(chat.replyto) != $null,$dccore.st(chat.replyto),*) }
  if (!$dccore.chat.relaying) { dccore.chat.sys Not connected to $iif($dccore.bot,$dccore.bot,the bot) $+ : the chat goes through it. /dccore connect | return }
  ; A channel target has to be one of the bot's own; a nick (private, #371
  ; follow-up) is checked by the bot instead, which is the one that knows
  ; who it has actually seen.
  if (%to != *) && ($left(%to,1) isin #&+!) && ($dccore.st(chat.chans) != $null) && (!$istok($dccore.st(chat.chans),%to,32)) { dccore.chat.sys $dccore.bot is not in %to $+ : pick one of its channels (right-click). | return }
  var %text = $strip($1-)
  if (%text == $null) { return }
  dccore.send chat %to %text
}
on *:INPUT:@DCCore-Chat: {
  ; Typing here is reading here, should on ACTIVE below not have fired.
  dccore.chat.unread.clear
  if ($left($1,1) == /) && ($left($1,2) != //) { return }
  dccore.chat.say $1-
  halt
}
; The window activated: everything in it has been seen (#1393). Any window,
; with $active checked here, rather than the window's name as the event's
; target: it holds whether mIRC matches that target against the window
; being activated or the one being left. An mIRC without on ACTIVE clears
; the count on the first line typed in the window instead (on INPUT above).
;
; mIRC runs only the FIRST matching event of a kind in one script file, and
; this one matches every window: anything else this file ever does on
; ACTIVE has to go into this handler, never into a second on ACTIVE below.
on *:ACTIVE:*: { if ($active == $dccore.chat.win) { dccore.chat.unread.clear } }
; mIRC back in the foreground with the chat window the active one: the
; count of what arrived while it was in the background goes (#1393). The
; same as on ACTIVE: the only on APPACTIVE this file may have.
on *:APPACTIVE: { if ($appactive) && ($active == $dccore.chat.win) { dccore.chat.unread.clear } }
on *:CLOSE:@DCCore-Chat: { if ($dccore.st(chat.unread) != $null) { hdel dccore.live chat.unread } }

; Choosing channels, from the window's right-click menu. Picking where to
; send also listens there: a channel you talk into and cannot hear back
; from is never what anybody wants.
alias dccore.chat.to {
  dccore.set chat.to $1
  if (!$istok($dccore.opt(chat.listen),$1,32)) { dccore.set chat.listen $addtok($dccore.opt(chat.listen),$1,32) }
  dccore.chat.title
  dccore.chat.sys Typing here now sends to $1 $+ , and $1 is listened on.
}
; Send to every channel where the bot has seen another DCCore bot (the default).
alias dccore.chat.to.all {
  dccore.set chat.to *
  dccore.chat.title
  dccore.chat.sys Typing here now sends to every channel with other DCCore bots.
}
alias dccore.chat.listen {
  if ($istok($dccore.opt(chat.listen),$1,32)) {
    dccore.set chat.listen $remtok($dccore.opt(chat.listen),$1,1,32)
    dccore.chat.sys No longer listening on $1 $+ .
  }
  else {
    dccore.set chat.listen $addtok($dccore.opt(chat.listen),$1,32)
    dccore.chat.sys Listening on $1 $+ .
  }
}
alias dccore.chat.all {
  dccore.set chat.all $iif($dccore.opt(chat.all),0,1)
  dccore.chat.sys $iif($dccore.opt(chat.all),Listening on every channel the bot is in.,Listening only on the channels ticked in this menu.)
}
; The bot's channels, N-th of them.
alias dccore.chat.chan { return $gettok($dccore.st(chat.chans),$1,32) }
; $submenu's rows: $1 is begin, then 1, 2, ... until an empty answer, then end.
;
; The COMMAND a row runs carries the channel's NUMBER, never its name (#955
; review): mIRC parses the command text when the row is clicked, and a
; channel name may contain | or $ - a channel with a hostile name could
; otherwise run a command of its choosing on the click. The number is
; resolved back to the channel inside the alias. The name is still shown as
; the row's label, which mIRC does not run.
alias dccore.chat.sendrow {
  if ($1 !isnum) { return }
  var %c = $dccore.chat.chan($1)
  if (%c == $null) { return }
  return $iif(%c == $dccore.opt(chat.to),$style(1)) %c $+ :dccore.chat.to.n $1
}
alias dccore.chat.listenrow {
  if ($1 !isnum) { return }
  var %c = $dccore.chat.chan($1)
  if (%c == $null) { return }
  return $iif($istok($dccore.opt(chat.listen),%c,32),$style(1)) %c $+ :dccore.chat.listen.n $1
}
alias dccore.chat.to.n { if ($1 isnum) && ($dccore.chat.chan($1) != $null) { dccore.chat.to $dccore.chat.chan($1) } }
alias dccore.chat.listen.n { if ($1 isnum) && ($dccore.chat.chan($1) != $null) { dccore.chat.listen $dccore.chat.chan($1) } }

; Message a peer privately (#371 follow-up): picked from the side-listbox, a
; line NUMBER only, never the nick itself - the same reason a channel picker
; carries a number (#955 review): the nick's own text is read back inside the
; alias with $line(), never re-parsed as a command.
alias dccore.chat.pickpeer {
  if ($1 !isnum) { return }
  var %nick = $line($dccore.chat.win,$1,1)
  if (%nick == $null) { return }
  dccore.set chat.to %nick
  dccore.chat.title
  dccore.chat.sys Typing here now sends privately to %nick $+ .
}

menu @DCCore-Chat {
  dclick:dccore.chat.pickpeer $1
  $iif(!$dccore.st(chat.chans),$dccore.mi(connect) (connect to the bot to see its channels)):dccore connect
  $dccore.mi(chat) Send to
  .$iif((!$dccore.opt(chat.to)) || ($dccore.opt(chat.to) == *),$style(1)) Every channel with other DCCore bots:dccore.chat.to.all
  .-
  .$submenu($dccore.chat.sendrow($1))
  $dccore.mi(hash) Listen on
  .$submenu($dccore.chat.listenrow($1))
  $iif($dccore.opt(chat.all),$style(1)) Listen on all the bot's channels:dccore.chat.all
  $iif($sline($dccore.chat.win,1),$dccore.mi(mail) Message this bot privately):dccore.chat.pickpeer $sline($dccore.chat.win,1).ln
  -
  $iif($dccore.opt(chat.history),$style(1)) Keep a history file:dccore.chat.hist.toggle
  $iif($dccore.opt(chat.flash),$style(1)) Flash on a new line:dccore.chat.flash.toggle
  $dccore.mi(folder) Open the history folder:dccore.chat.hist.open
  -
  $dccore.mi(clear) Clear window:clear $dccore.chat.win
  $dccore.mi(settings) Control Panel...:dccore.settings
}

; ---- History and unread (#1393) ---------------------------------------

; The folder, in mIRC's own log folder ($logdir, which ends with "\"), and
; the files in it. A line's file (#1401): $1 its channel as the bot sent it,
; $2 its id - a folder per channel, a file per month ("2026-10.log"), so the
; history's limits delete whole months (dccore.chat.hist.prune) instead of
; rewriting a file that only grows. A channel's folder is its name made
; safe for a file name ($mkfn); a private conversation's is its "@<nick>"
; channel the same way - the "@" also keeps a nick such as CON or NUL from
; naming a Windows device; and your own line said in several channels at
; once ("-") goes to every-channel. Paths are quoted ($qt) wherever a
; command takes one.
alias dccore.chat.hist.dir { return $+($logdir,DCCore-Chat) }
alias dccore.chat.hist.recent { return $+($dccore.chat.hist.dir,\recent.txt) }
alias dccore.chat.hist.file {
  var %who = $iif($1 == -,every-channel,$mkfn($1))
  return $+($dccore.chat.hist.dir,\,%who,\,$asctime($int($calc($2 / 1000)),yyyy-mm),.log)
}
; How many lines of recent.txt the window replays. The file is cut back to
; that many once it holds twice as many.
alias dccore.chat.hist.keep { return 200 }
; The hidden window the trim and the replay load recent.txt into.
alias dccore.chat.hist.buf { return @dccore.chat.buf }
; The newest id the file may raise chat.last to: five minutes past the
; clock. One further ahead - from a bot whose clock ran fast, or from before
; this PC's clock was put back - would hold chat.last above every line to
; come, and the window would draw and keep nothing more.
alias dccore.chat.hist.clock { return $calc(($ctime + 300) * 1000) }

; The folder, made when it is missing - and mIRC's log folder before it,
; which is only there once something has been logged. $true when it is there.
alias dccore.chat.hist.mkdir {
  var %d = $dccore.chat.hist.dir
  if ($isdir(%d)) { return $true }
  if (!$isdir($logdir)) { mkdir $qt($left($logdir,-1)) }
  mkdir $qt(%d)
  return $isdir(%d)
  :error
  reseterror
  return $false
}

; A line the window drew: $1 its id, $2 its channel as the bot sent it, $3
; the nick, $4 own or other, $5- the text. Nothing while the history is off.
; A file that cannot be written loses the line, and nothing else: the
; error stops here, not in the feed.
alias dccore.chat.hist.add {
  if (!$dccore.opt(chat.history)) { return }
  if (!$dccore.chat.hist.mkdir) { return }
  var %f = $dccore.chat.hist.file($2,$1)
  if (!$isdir($nofile(%f))) { mkdir $qt($left($nofile(%f),-1)) }
  write $qt(%f) $asctime($int($calc($1 / 1000)),yyyy-mm-dd HH:nn:ss) $iif($2 == -,*,$2) $3 $strip($5-)
  var %r = $dccore.chat.hist.recent
  write $qt(%r) $1 $2 $3 $4 $strip($5-)
  if ($lines(%r) > $calc(2 * $dccore.chat.hist.keep)) { dccore.chat.hist.trim }
  return
  :error
  reseterror
}

; recent.txt cut back to its last lines, through a hidden window: /loadbuf N
; loads the LAST N lines of a file, and /savebuf writes them back over it.
alias dccore.chat.hist.trim {
  var %r = $dccore.chat.hist.recent
  var %w = $dccore.chat.hist.buf
  if ($window(%w)) { window -c %w }
  window -h %w
  loadbuf $dccore.chat.hist.keep %w $qt(%r)
  savebuf %w $qt(%r)
  window -c %w
}

; The last lines of recent.txt, drawn in grey when the window opens, by the
; rules a live line is drawn by (dccore.chat.feed): a channel listened on, a
; private line, your own. Drawn with a plain echo - never written again,
; never counted as unread, and no -m, so the button is not lit for them.
; chat.last moves to the newest id read, so the bot's own replay after HELLO
; cannot draw one of them a second time - but not past the clock
; (dccore.chat.hist.clock): a line from the future is drawn, and moves nothing.
alias dccore.chat.hist.replay {
  if (!$dccore.opt(chat.history)) { return }
  var %r = $dccore.chat.hist.recent
  if (!$isfile(%r)) { return }
  var %w = $dccore.chat.hist.buf
  if ($window(%w)) { window -c %w }
  window -h %w
  loadbuf $dccore.chat.hist.keep %w $qt(%r)
  var %top = $dccore.st(chat.last)
  var %clock = $dccore.chat.hist.clock
  var %shown = 0
  var %i = 1
  while (%i <= $line(%w,0)) {
    tokenize 32 $line(%w,%i)
    if ($1 isnum) && ($4 != $null) {
      if ($1 <= %clock) && ((%top !isnum) || ($1 > %top)) { var %top = $1 }
      if ($4 == own) || ($left($2,1) == @) || ($dccore.chat.listens($2)) {
        if (!%shown) { dccore.chat.sys Earlier, from the history file: | var %shown = 1 }
        echo 14 -i2 $dccore.chat.win $+($dccore.chat.hist.when($1),$chr(160),$iif($2 == -,*,$2)) $3 $dccore.chat.cut($5-)
      }
    }
    inc %i
  }
  window -c %w
  if (%shown) { dccore.chat.sys End of the history file. }
  if (%top isnum) { hadd dccore.live chat.last %top }
  return
  :error
  reseterror
  if ($window($dccore.chat.hist.buf)) { window -c $dccore.chat.hist.buf }
}
; A replayed line's time, from its id: the time alone for today, the date
; and the time before that.
alias dccore.chat.hist.when {
  var %t = $int($calc($1 / 1000))
  if ($asctime(%t,yyyymmdd) == $asctime($ctime,yyyymmdd)) { return $asctime(%t,HH:nn) }
  return $asctime(%t,yyyy-mm-dd HH:nn)
}
; The newest id in recent.txt, for the first CHAT line of a session: that
; line was drawn, and written, in an earlier one. Not one from the future
; (dccore.chat.hist.clock).
alias dccore.chat.hist.floor {
  if (!$dccore.opt(chat.history)) { return }
  var %r = $dccore.chat.hist.recent
  if (!$isfile(%r)) { return }
  var %n = $lines(%r)
  if (%n < 1) { return }
  var %id = $gettok($read(%r,nt,%n),1,32)
  if (%id isnum) && (%id <= $dccore.chat.hist.clock) { hadd dccore.live chat.last %id }
  return
  :error
  reseterror
}

; A line from another bot, drawn while the window is not the active one -
; or while it is, with mIRC itself in the background, where $active stays
; the window you left: counted in the title, and with "Flash on a new line"
; ticked /flash, which flashes mIRC in the taskbar only while mIRC is in
; the background. The switchbar button is lit by the line itself
; (dccore.chat.show's echo -m), as before.
alias dccore.chat.unread.add {
  if ($active == $dccore.chat.win) && ($appactive) { return }
  hinc dccore.live chat.unread
  dccore.chat.title
  if ($dccore.opt(chat.flash)) { flash $dccore.chat.win DCCore Chat $dccore.dot $dccore.st(chat.unread) unread }
}
alias dccore.chat.unread.clear {
  if ($dccore.st(chat.unread) == $null) { return }
  hdel dccore.live chat.unread
  dccore.chat.title
}

; The right-click menu's three.
; ---- The history's limits (#1401) -------------------------------------

; DCCORE CHATHIST <days> <max_mb>, the bot's CHAT_HISTORY_DAYS and
; CHAT_HISTORY_MAX_MB: kept in dccore.ini, and acted on at once when they
; changed. Anything but two whole numbers of 0 or more is ignored.
alias dccore.chat.hist.limits {
  if ($1 !isnum) || ($2 !isnum) { return }
  var %days = $int($1)
  var %mb = $int($2)
  if (%days < 0) || (%mb < 0) { return }
  if (%days == $dccore.opt(chat.hist.days)) && (%mb == $dccore.opt(chat.hist.mb)) { return }
  hadd dccore chat.hist.days %days
  hadd dccore chat.hist.mb %mb
  dccore.save
  dccore.chat.hist.prune
}
; Once a day at most, when the chat window opens: the limits applied. The
; day is marked whatever the pass finds - no limits yet, no folder - so the
; window opens without it again until tomorrow.
alias dccore.chat.hist.daily {
  if ($dccore.st(chat.pruned) == $asctime($ctime,yyyymmdd)) { return }
  hadd dccore.live chat.pruned $asctime($ctime,yyyymmdd)
  dccore.chat.hist.prune
}
; The month files that are over the limits, deleted: every month that ended
; more than the days ago - a month before the one the limit falls in ended
; before it - then, while the folder is over the size, the oldest months,
; never the month running. Only files named for a month ("2026-10.log") in
; the history folder and its channel folders are looked at; recent.txt and
; anything else there is not touched. Nothing at all before a bot has sent
; the limits, or while both are 0. A file that cannot be deleted stops this
; pass, and the next one tries again.
alias dccore.chat.hist.prune {
  var %days = $dccore.opt(chat.hist.days)
  var %mb = $dccore.opt(chat.hist.mb)
  if (%days !isnum) { var %days = 0 }
  if (%mb !isnum) { var %mb = 0 }
  if (%days <= 0) && (%mb <= 0) { return }
  var %dir = $dccore.chat.hist.dir
  if (!$isdir(%dir)) { return }
  if ($hget(dccore.chathist)) { hfree dccore.chathist }
  hmake dccore.chathist 100
  hadd dccore.chathist n 0
  noop $findfile(%dir,????-??.log,0,2,dccore.chat.hist.seen $1-)
  var %months = $null
  var %total = 0
  var %i = 1
  while (%i <= $hget(dccore.chathist,n)) {
    var %row = $hget(dccore.chathist,f $+ %i)
    var %total = $calc(%total + $gettok(%row,2,32))
    var %months = $addtok(%months,$gettok(%row,1,32),32)
    inc %i
  }
  var %months = $sorttok(%months,32,n)
  if (%days > 0) {
    var %cut = $asctime($calc($ctime - %days * 86400),yyyymm)
    var %j = 1
    while (%j <= $numtok(%months,32)) {
      if ($gettok(%months,%j,32) < %cut) { var %total = $calc(%total - $dccore.chat.hist.drop($gettok(%months,%j,32))) }
      inc %j
    }
  }
  if (%mb > 0) {
    var %max = $calc(%mb * 1048576)
    var %now = $asctime($ctime,yyyymm)
    var %j = 1
    while (%total > %max) && (%j <= $numtok(%months,32)) && ($gettok(%months,%j,32) < %now) {
      var %total = $calc(%total - $dccore.chat.hist.drop($gettok(%months,%j,32)))
      inc %j
    }
  }
  hfree dccore.chathist
  return
  :error
  reseterror
  if ($hget(dccore.chathist)) { hfree dccore.chathist }
}
; A file $findfile found, kept as "<yyyymm> <bytes> <path>" when its name is
; a month's; anything else in the folder is passed over.
alias dccore.chat.hist.seen {
  var %name = $nopath($1-)
  var %m = $remove(%name,-,.log)
  if ($len(%name) != 11) || (%m !isnum) { return }
  hinc dccore.chathist n
  hadd dccore.chathist f $+ $hget(dccore.chathist,n) %m $file($1-).size $1-
}
; Month $1 (yyyymm) deleted, every channel's file of it: the bytes it held.
alias dccore.chat.hist.drop {
  var %freed = 0
  var %i = 1
  while (%i <= $hget(dccore.chathist,n)) {
    var %row = $hget(dccore.chathist,f $+ %i)
    if ($gettok(%row,1,32) == $1) {
      .remove $qt($gettok(%row,3-,32))
      var %freed = $calc(%freed + $gettok(%row,2,32))
      hdel dccore.chathist f $+ %i
    }
    inc %i
  }
  return %freed
}

alias dccore.chat.hist.toggle {
  dccore.set chat.history $iif($dccore.opt(chat.history),0,1)
  dccore.chat.sys $iif($dccore.opt(chat.history),Keeping a history file in $dccore.chat.hist.dir $+ .,No longer keeping a history file: nothing more is written to it and nothing is replayed. The files already written stay, until the history's limits remove them.)
}
alias dccore.chat.flash.toggle {
  dccore.set chat.flash $iif($dccore.opt(chat.flash),0,1)
  dccore.chat.sys $iif($dccore.opt(chat.flash),mIRC flashes in the taskbar for a new line while it is in the background.,mIRC no longer flashes in the taskbar for a new line. The unread count in the title and the lit button stay.)
}
alias dccore.chat.hist.open {
  if (!$dccore.chat.hist.mkdir) { dccore.chat.sys Could not make the history folder $dccore.chat.hist.dir $+ . | return }
  run $qt($dccore.chat.hist.dir)
}

; A PEERS line: the DCCore bots WHO found in one channel, right now - kept
; per channel so a channel the bot leaves does not linger in the list.
alias dccore.chat.peerline {
  if ($1 == $null) { return }
  if ($2-) { hadd dccore.chatpeers $1 $2- }
  else { hdel dccore.chatpeers $1 }
  dccore.chat.peers.redraw
}
; The side-listbox: every bot WHO has found, in any of the bot's channels -
; deduped, since a peer usually shares more than one with us - and sorted.
; $addtok() itself never appends a token already in the list - documented
; behaviour, not something built here by hand out of a second hash table
; whose own create/free lifecycle turned out to be the wrong place to
; look for exactness (seen live: three rows for one peer in three
; channels, with the hash-table version this replaces).
alias dccore.chat.peers.redraw {
  if (!$window($dccore.chat.win)) { return }
  var %sorted = $null
  var %i = 1
  while ($gettok($dccore.st(chat.chans),%i,32) != $null) {
    var %have = $hget(dccore.chatpeers,$gettok($dccore.st(chat.chans),%i,32))
    var %j = 1
    while ($gettok(%have,%j,32) != $null) {
      var %sorted = $addtok(%sorted,$gettok(%have,%j,32),32)
      inc %j
    }
    inc %i
  }
  if (%sorted != $null) { var %sorted = $sorttok(%sorted,32) }
  ; dline -l <win> 1-N (one /dline call, a hyphenated range) turned out to be
  ; a silent no-op on this listbox - confirmed live: a peer's rows stayed at
  ; their old count and only ever grew, redraw after redraw, even the moment
  ; after that peer had actually quit and $addtok's own list no longer had
  ; it. One line at a time avoids the range form entirely and only depends
  ; on a single dline -l deletion, which the peer-picker's $line(...,N,1)
  ; read side already proves this window's listbox honours.
  while ($line($dccore.chat.win,0,1) > 0) { dline -l $dccore.chat.win 1 }
  var %k = 1
  while ($gettok(%sorted,%k,32) != $null) {
    aline -l $dccore.chat.win $gettok(%sorted,%k,32)
    inc %k
  }
}
