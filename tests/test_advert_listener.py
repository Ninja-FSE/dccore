"""Reading the periodic advert every file-serving bot sends to the channel.

The daemon has never done this. It parses @find reply headers, which arrive
only when we broadcast, and nothing watched ordinary channel traffic - so it
knew nothing about who else serves files, what they hold, or whether the list
we hold for them is still current.

THE FIXTURES ARE REAL

Every advert below was captured from a busy public file-sharing channel by a
read-only observer: 392 lines, 33 bots, twenty minutes. They are reproduced as they
arrived, not tidied, because the untidiness is the point - no two bots format
alike, and a parser written against a cleaned-up sample is a parser written
against a bot nobody runs.

One transformation was applied, and only because the daemon applies it first.
Several bots decorate their fields with bytes that are not valid UTF-8, and
irc.py reads the socket with errors="ignore" - so those bytes never reach the
parser. Those adverts appear here as the daemon actually sees them, separators
already gone. The "\\x95" you will see is the other half of that story: a
cp1252 bullet that arrived double-encoded, making it a valid UTF-8 control
character, so it survives the decode and strip_control_codes() leaves it be.

WHAT THE SAMPLE SETTLES

  * An advert is not always one message. Five of the 33 split theirs across two
    PRIVMSGs at IRC's 512-byte line limit, and Echosonic's break lands exactly
    on its list date - the one field the whole freshness signal depends on.
  * There are three wordings, not one: the "Type: @nick" of OmenServe and its
    relatives, SPQR's "For My List(19527files:163812MB)", and a separate
    RAR-folder list advertised under a "^" trigger.
  * The labels are stable, the layout is not.
  * A list DATE comes from the OmenServe family. A list SIZE comes from SPQR,
    from the RAR advert, and from DCCore and TestBot - so nothing may depend on
    either being there.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import db  # noqa: E402
import irc  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

# A fixed clock. Every test that cares about the continuation window passes an
# explicit `now`, so none of this depends on how fast the suite runs.
T0 = 1000000.0

# CTCP messages are wrapped in this byte.
CTCP = chr(1)


# Captured from a busy public file-sharing channel - 392 lines, 33 bots. Verbatim,
# save for the bytes the socket's errors="ignore" decode drops before the
# parser ever sees them. scripts/capture_adverts.py took the sample.

RIGSERV = (
    '\x0313,1127,500 full albums in .rar\x0301,11: \x0310,11Type:\x0313,11 '
    '@[rigserv] \x0310,11For My List Of:\x0313,11 77,018 \x0310,11Files '
    '\x0301,11: \x0310,11Slots:\x0313,11 6/6 \x0301,11: \x0310,11Queued:\x0313,11 0 '
    '\x0301,11: \x0310,11Speed:\x0313,11 0cps \x0301,11: \x0310,11Next: \x0313,11NOW '
    '\x0301,11: \x0310,11Served:\x0313,11 1,624,853 \x0301,11: \x0310,11List: '
    '\x0313,11Aug 29th \x0301,11: \x0310,11Search: \x0313,11ON \x0301,11: '
    '\x0310,11Mode: \x0313,11Normal \x0301,11:'
)

GLIDER = (
    '\x0308,12\x0314,12 \x0300,12Type:\x0308,12 @`Glider \x0300,12For My List '
    'Of:\x0308,12 1,116,789 \x0300,12Files \x0314,12 \x0300,12Slots:\x0308,12 4/4 '
    '\x0314,12 \x0300,12Queued:\x0308,12 0 \x0314,12 \x0300,12Speed:\x0308,12 0cps '
    '\x0314,12 \x0300,12Next: \x0308,12NOW \x0314,12 \x0300,12Served:\x0308,12 584,496 '
    '\x0314,12 \x0300,12List: \x0308,12Jul 16th \x0314,12 \x0300,12Search: \x0308,12ON '
    '\x0314,12 \x0300,12Mode: \x0308,12Normal \x0314,12'
)

AVENIO = (
    '\x0308,06\x0301,04 ...All... \x0301,08 ...For... \x0301,04 ...You...\x0309,06\xa9 '
    '\x0300,06Type:\x0308,06 @AvenIo \x0300,06For My List Of:\x0308,06 150,623 '
    '\x0300,06Files \x0309,06\xa9 \x0300,06Slots:\x0308,06 2/3 \x0309,06\xa9 '
    '\x0300,06Queued:\x0308,06 4 \x0309,06\xa9 \x0300,06Speed:\x0308,06 0cps \x0309,06\xa9 '
    '\x0300,06Next: \x0308,06NOW \x0309,06\xa9 \x0300,06Served:\x0308,06 3,948,649 '
    '\x0309,06\xa9 \x0300,06List: \x0308,06Jul 8th \x0309,06\xa9 \x0300,06Search: '
    '\x0308,06ON \x0309,06\xa9 \x0300,06Mode:'
)

AVENIO_TAIL = '\x030,6\x0308,06Normal \x0309,06\xa9'

ADITERRA = (
    '\x0304,00\x0314,00\x95 \x0312,00Type:\x0304,00 @aditerra \x0312,00For My List '
    'Of:\x0304,00 184,437 \x0312,00Files \x0314,00\x95 \x0312,00Slots:\x0304,00 4/4 '
    '\x0314,00\x95 \x0312,00Queued:\x0304,00 0 \x0314,00\x95 \x0312,00Speed:\x0304,00 0cps '
    '\x0314,00\x95 \x0312,00Next: \x0304,00NOW \x0314,00\x95 \x0312,00Served:\x0304,00 '
    '288,860 \x0314,00\x95 \x0312,00List: \x0304,00Aug 28th \x0314,00\x95 '
    '\x0312,00Search: \x0304,00ON \x0314,00\x95 \x0312,00Mode: \x0304,00Normal \x0314,00\x95'
)

WREN = (
    '\x0312,13\x0304,13~ \x0308,13Type:\x0312,13 @Wren \x0308,13For My List '
    'Of:\x0312,13 389,136 \x0308,13Files \x0304,13~ \x0308,13Slots:\x0312,13 2/2 '
    '\x0304,13~ \x0308,13Queued:\x0312,13 1 \x0304,13~ \x0308,13Speed:\x0312,13 0cps '
    '\x0304,13~ \x0308,13Next: \x0312,13NOW \x0304,13~ \x0308,13Served:\x0312,13 '
    '30,753 \x0304,13~ \x0308,13List: \x0312,13Jun 20th \x0304,13~ \x0308,13Search: '
    '\x0312,13ON \x0304,13~ \x0308,13Mode: \x0312,13Normal \x0304,13~'
)

KELTA = (
    '\x0301,08\x038,4(`.\x033\x038\x033\x03\x034,8* Musique Pour Tous '
    '*\x038,4\x033\x038\x033\x038,4.)\x0308,08 \x0301,08Type:\x0301,08 @Kelta \x0301,08For My '
    'List Of:\x0301,08 391,854 \x0301,08Files \x0308,08 \x0301,08Slots:\x0301,08 '
    '5/5 \x0308,08 \x0301,08Queued:\x0301,08 0 \x0308,08 \x0301,08Speed:\x0301,08 0cps '
    '\x0308,08 \x0301,08Next: \x0301,08NOW \x0308,08 \x0301,08Served:\x0301,08 410,186 '
    '\x0308,08 \x0301,08List: \x0301,08Aug 26th \x0308,08 \x0301,08Search: '
    '\x0301,08OFF \x0308,08 \x0301,08Mode: \x0301,08Normal \x0308,08'
)

BIGRIG = (
    '\x032,14 For \x035My List\x032(\x0311\x02\x0219527\x032files:\x0311\x02\x02163812\x032MB) and '
    '\x035DCC Status\x032, type \x035@BigRig \x032and \x035@BigRig-stats\x032. '
    '[(\x0311\x02\x020\x032/\x02\x027) Slots (\x0311\x02\x020\x032/\x02\x02216) Ques Taken] [Next Send: '
    '\x0311\x02\x02Open Slot Ready\x032] [CPS in Use: \x0311\x02\x020\x032] [Highest Cps '
    'Record: \x0311\x02\x024788.6\x032 by \x0311FtpBandit\x032] [Total Files Served: '
    '\x0311\x02\x02755993\x032] \x03'
)

COBRRT = (
    '\x0304,00\x0314,00\x95 \x0312,00Type:\x0304,00 @cobrrt \x0312,00For My List '
    'Of:\x0304,00 32,119 \x0312,00Files \x0314,00\x95 \x0312,00Slots:\x0304,00 5/5 '
    '\x0314,00\x95 \x0312,00Queued:\x0304,00 0 \x0314,00\x95 \x0312,00Speed:\x0304,00 0cps '
    '\x0314,00\x95 \x0312,00Next: \x0304,00NOW \x0314,00\x95 \x0312,00Served:\x0304,00 796 '
    '\x0314,00\x95 \x0312,00List: \x0304,00Aug 18th \x0314,00\x95 \x0312,00Search: '
    '\x0304,00ON \x0314,00\x95 \x0312,00Mode: \x0304,00Normal \x0314,00\x95'
)

ZKX = (
    '\x0304,00\x0314,00\x95 \x0312,00Type:\x0304,00 @Zkx \x0312,00For My List '
    'Of:\x0304,00 719,041 \x0312,00Files \x0314,00\x95 \x0312,00Slots:\x0304,00 10/10 '
    '\x0314,00\x95 \x0312,00Queued:\x0304,00 0 \x0314,00\x95 \x0312,00Speed:\x0304,00 0cps '
    '\x0314,00\x95 \x0312,00Next: \x0304,00NOW \x0314,00\x95 \x0312,00Served:\x0304,00 '
    '3,456,016 \x0314,00\x95 \x0312,00List: \x0304,00Aug 10th \x0314,00\x95 '
    '\x0312,00Search: \x0304,00ON \x0314,00\x95 \x0312,00Mode: \x0304,00Normal \x0314,00\x95'
)

ZKX_RAR = (
    '\x0315,02 Type\x0309,02 @Zkx^ \x0315,02to get my list of \x0309,0239,454 '
    '(5.48 TB) \x0315,02RAR folders \x0304,02\u25a0\x0315,02 Available slots: '
    '\x0309,0228/29 \x0304,02\u25a0\x0315,02 Folders in queue: \x0309,020 '
    '\x0304,02\u25a0\x0315,02 Folders sent: \x0309,0211,334 \x0304,02\u25a0\x0315,02 Transfer '
    'speed: \x0309,020 KB/s \x0304,02\u25a0\x0315,02 Updated list: \x0309,0214/Aug/26 '
    '\x0304,02\u25a0\x0304,02 [ \x0309,02mx.rarserver v1.0.6\x0304,02 ] \x03'
)

DEN = (
    '\x0304,00This Space for Rent\x0314,00 \x0312,00Type:\x0304,00 @Den '
    '\x0312,00For My List Of:\x0304,00 10,965 \x0312,00Files \x0314,00 '
    '\x0312,00Slots:\x0304,00 4/4 \x0314,00 \x0312,00Queued:\x0304,00 0 \x0314,00 '
    '\x0312,00Speed:\x0304,00 0cps \x0314,00 \x0312,00Next: \x0304,00NOW \x0314,00 '
    '\x0312,00Served:\x0304,00 25,511 \x0314,00 \x0312,00List: \x0304,00Aug 29th '
    '\x0314,00 \x0312,00Search: \x0304,00ON \x0314,00 \x0312,00Mode: \x0304,00Normal '
    '\x0314,00'
)

Q_R_Q = (
    '\x0309,01 \x0304,01\xa5 \x0300,01Type:\x0309,01 @Q_R_Q \x0300,01For My List '
    'Of:\x0309,01 127,312 Files \x0304,01\xa5 \x0300,01Utilization:\x0309,01 0.94TB '
    '/ 17.75TB \x0304,01\xa5 \x0300,01Speed:\x0309,01 0cps \x0304,01\xa5 '
    '\x0300,01Served:\x0309,01 283,570 \x0304,01\xa5 \x0300,01List: \x0309,01Aug 28th '
    '\x0304,01\xa5 \x0300,01Parallel Sends: \x0309,01OFF \x0304,01\xa5 \x0300,01Mode: '
    '\x0309,01Normal \x0304,01\xa5'
)

DCCORE = (
    '\x0304,05 \x0310,10 \x0301,00 Type: \x02\x0303@DCCore\x0f\x0301,00 For My List Of: '
    '\x02\x030461,101\x0f\x0301,00 Files (1.84TB) created \x0303Aug 27th \x0310,10 '
    '\x0304,05 \x0301,00 Slots: 3/3 \x0310,10 \x0304,05 \x0301,00 Queued: 0 \x0310,10 '
    '\x0304,05 \x0301,00 Speed: 0k/s / Record: 53.59MB/s \x0310,10 \x0304,05 '
    '\x0301,00 Total Sent: 2,333 Files (254.0GB) \x0310,10 \x0304,05 \x0301,00 '
    'Search: \x02\x0303ON\x0f\x0301,00 \x0310,10 \x0304,05 \x0301,00 DCCore v1.10.0-RC3 '
    '\x0310,10 \x0304,05'
)

DCCOREWEB = (
    '\x0304,05 \x0310,10 \x0301,00 Type: \x02\x0303@DCCoreWeb\x0f\x0301,00 For My List '
    'Of: \x02\x030461,156\x0f\x0301,00 Files (1.84TB) created \x0303Aug 28th \x0310,10 '
    '\x0304,05 \x0301,00 Slots: 3/3 \x0310,10 \x0304,05 \x0301,00 Queued: 0 \x0310,10 '
    '\x0304,05 \x0301,00 Speed: 0k/s / Record: 0k/s \x0310,10 \x0304,05 \x0301,00 '
    'Total Sent: 35 Files (776.0MB) \x0310,10 \x0304,05 \x0301,00 Search: '
    '\x02\x0303ON\x0f\x0301,00 \x0310,10 \x0304,05 \x0301,00 DCCore v1.10.0-RC4 \x0310,10 '
    '\x0304,05'
)

DCCOREWIN = (
    '\x0304,05 \x0310,10 \x0301,00 Type: \x02\x0303@DCCoreWin\x0f\x0301,00 For My List '
    'Of: \x02\x030436,208\x0f\x0301,00 Files (1.21TB) created \x0303Aug 27th \x0310,10 '
    '\x0304,05 \x0301,00 Slots: 3/3 \x0310,10 \x0304,05 \x0301,00 Queued: 0 \x0310,10 '
    '\x0304,05 \x0301,00 Speed: 0k/s / Record: 0k/s \x0310,10 \x0304,05 \x0301,00 '
    'Total Sent: 31 Files (833.1MB) \x0310,10 \x0304,05 \x0301,00 Search: '
    '\x02\x0303ON\x0f\x0301,00 \x0310,10 \x0304,05 \x0301,00 DCCore v1.10.0-RC4 \x0310,10 '
    '\x0304,05'
)

DEEPCOVE = (
    '\x0308,02 Fios - \x0311,02Type:\x0308,02 @Deepcove \x0311,02For My List '
    'Of:\x0308,02 288,090 \x0311,02Files \x0304,02 \x0311,02Slots:\x0308,02 6/6 '
    '\x0304,02 \x0311,02Queued:\x0308,02 0 \x0304,02 \x0311,02Speed:\x0308,02 0cps '
    '\x0304,02 \x0311,02Next: \x0308,02NOW \x0304,02 \x0311,02Served:\x0308,02 '
    '1,136,753 \x0304,02 \x0311,02List: \x0308,02Apr 29th \x0304,02 '
    '\x0311,02Search: \x0308,02OFF \x0304,02 \x0311,02Mode: \x0308,02Normal \x0304,02'
)

DJPHANTOM = (
    "\x0304,00Hello friend if you can't download please active the "
    'passive DCC\x0314,00\x95 \x0312,00Type:\x0304,00 @DJPhantom \x0312,00For My '
    'List Of:\x0304,00 101,736 \x0312,00Files \x0314,00\x95 \x0312,00Slots:\x0304,00 '
    '20/20 \x0314,00\x95 \x0312,00Queued:\x0304,00 0 \x0314,00\x95 \x0312,00Speed:\x0304,00 '
    '0cps \x0314,00\x95 \x0312,00Next: \x0304,00NOW \x0314,00\x95 \x0312,00Served:\x0304,00 '
    '505 \x0314,00\x95 \x0312,00List: \x0304,00Aug 25th \x0314,00\x95 \x0312,00Search: '
    '\x0304,00ON \x0314,00\x95'
)

DJPHANTOM_TAIL = '\x0314,0\x0312,00Mode: \x0304,00Normal \x0314,00\x95'

FALLBACK77 = (
    '\x0304,01an OP heLped me fix this NO OS LiMiTS... xD\x0314,01\x95 '
    '\x0315,01Type:\x0304,01 @fallback77 \x0315,01For My List Of:\x0304,01 '
    '39,659 \x0315,01Files \x0314,01\x95 \x0315,01Slots:\x0304,01 2/2 \x0314,01\x95 '
    '\x0315,01Queued:\x0304,01 0 \x0314,01\x95 \x0315,01Speed:\x0304,01 0cps \x0314,01\x95 '
    '\x0315,01Next: \x0304,01NOW \x0314,01\x95 \x0315,01Served:\x0304,01 2,810 \x0314,01\x95 '
    '\x0315,01List: \x0304,01Aug 29th \x0314,01\x95 \x0315,01Search: \x0304,01ON '
    '\x0314,01\x95 \x0315,01Mode:'
)

FALLBACK77_TAIL = '\x0315,1\x0304,01Servers Priority \x0314,01\x95'

ICEWOLFZZZ = (
    '\x0304,15\x034,15 \x0310\x031 \x034\x0314,15 \x0301,15Type:\x0304,15 @IceWolfzzz '
    '\x0301,15For My List Of:\x0304,15 308,738 \x0301,15Files \x0314,15 '
    '\x0301,15Slots:\x0304,15 3/3 \x0314,15 \x0301,15Queued:\x0304,15 0 \x0314,15 '
    '\x0301,15Speed:\x0304,15 0cps \x0314,15 \x0301,15Next: \x0304,15NOW \x0314,15 '
    '\x0301,15Served:\x0304,15 1,105,288 \x0314,15 \x0301,15List: \x0304,15Aug 24th '
    '\x0314,15 \x0301,15Search: \x0304,15OFF \x0314,15 \x0301,15Mode: \x0304,15Normal '
    '\x0314,15'
)

AUDIOVAULT = (
    '\x0309,06Flac & mp3 Oldies \x0314,06\x95 \x0308,06Type:\x0309,06 @AudioVault '
    '\x0308,06For My List Of:\x0309,06 313,435 Files \x0314,06\x95 '
    '\x0308,06Utilization:\x0309,06 4.79331TB / 7.74221TB \x0314,06\x95 '
    '\x0308,06Speed:\x0309,06 0cps \x0314,06\x95 \x0308,06Served:\x0309,06 50,880 '
    '\x0314,06\x95 \x0308,06List: \x0309,06Jan 2nd \x0314,06\x95 \x0308,06Parallel Sends: '
    '\x0309,06OFF \x0314,06\x95 \x0308,06Mode: \x0309,06Servers Priority \x0314,06\x95'
)

PHONEZONE = (
    '\x0304,00\x0314,00\x95 \x0312,00Type:\x0304,00 @PhoneZone \x0312,00For My List '
    'Of:\x0304,00 147,636 \x0312,00Files \x0314,00\x95 \x0312,00Slots:\x0304,00 1/1 '
    '\x0314,00\x95 \x0312,00Queued:\x0304,00 0 \x0314,00\x95 \x0312,00Speed:\x0304,00 0cps '
    '\x0314,00\x95 \x0312,00Next: \x0304,00NOW \x0314,00\x95 \x0312,00Served:\x0304,00 954 '
    '\x0314,00\x95 \x0312,00List: \x0304,00Aug 28th \x0314,00\x95 \x0312,00Search: '
    '\x0304,00OFF \x0314,00\x95 \x0312,00Mode: \x0304,00Normal \x0314,00\x95'
)

TERAVISTA = (
    '\x0308,01SERVEUR EN FRANCAIS '
    'UNIQUEMENT...FILMS,SERIES,VIDEOCLIPS,ETC...\x0314,01\x95 '
    '\x0300,01Type:\x0308,01 @Teravista \x0300,01For My List Of:\x0308,01 96,371 '
    '\x0300,01Files \x0314,01\x95 \x0300,01Slots:\x0308,01 5/5 \x0314,01\x95 '
    '\x0300,01Queued:\x0308,01 0 \x0314,01\x95 \x0300,01Speed:\x0308,01 0cps \x0314,01\x95 '
    '\x0300,01Next: \x0308,01NOW \x0314,01\x95 \x0300,01Served:\x0308,01 1,141 \x0314,01\x95 '
    '\x0300,01List: \x0308,01Jul 29th \x0314,01\x95 \x0300,01Search: \x0308,01ON '
    '\x0314,01\x95'
)

TERAVISTA_TAIL = '\x0314,1\x0300,01Mode: \x0308,01Servers Only \x0314,01\x95'

TERAVISTA_RAR = (
    '\x0308,01 Type\x0309,01 @Teravista^ \x0308,01to get my list of '
    '\x0309,0121,760 (836.01 GB) \x0308,01RAR folders \x0303,01\u26a1\x0308,01 '
    'Available slots: \x0309,015/5 \x0303,01\u26a1\x0308,01 Folders in queue: '
    '\x0309,010 \x0303,01\u26a1\x0308,01 Folders sent: \x0309,01335 \x0303,01\u26a1\x0308,01 '
    'Transfer speed: \x0309,010 KB/s \x0303,01\u26a1\x0308,01 Updated list: '
    '\x0309,0129/Jun/26 \x0303,01\u26a1\x0303,01 [ \x0309,01mx.rarserver v1.0.6\x0303,01 '
    '] \x03'
)

OAKWOOD = (
    '\x0312,15\x02\x032,15<<<\x0312,15M\xe8N\xe1Ce\x032,15>>>\x02\x0314,15\x95 \x0302,15Type:\x0312,15 '
    '@Oakwood \x0302,15For My List Of:\x0312,15 34,842 \x0302,15Files \x0314,15\x95 '
    '\x0302,15Slots:\x0312,15 3/3 \x0314,15\x95 \x0302,15Queued:\x0312,15 0 \x0314,15\x95 '
    '\x0302,15Speed:\x0312,15 0cps \x0314,15\x95 \x0302,15Next: \x0312,15NOW \x0314,15\x95 '
    '\x0302,15Served:\x0312,15 3,833 \x0314,15\x95 \x0302,15List: \x0312,15Jul 31st '
    '\x0314,15\x95 \x0302,15Search: \x0312,15ON \x0314,15\x95 \x0302,15Mode: \x0312,15Normal '
    '\x0314,15\x95'
)

NORTHVALE = (
    '\x0304,01\x0309,01 \x0300,01Type:\x0304,01 @Northvale \x0300,01For My List '
    'Of:\x0304,01 455,929 \x0300,01Files \x0309,01 \x0300,01Slots:\x0304,01 5/5 '
    '\x0309,01 \x0300,01Queued:\x0304,01 0 \x0309,01 \x0300,01Speed:\x0304,01 0cps '
    '\x0309,01 \x0300,01Next: \x0304,01NOW \x0309,01 \x0300,01Served:\x0304,01 82 '
    '\x0309,01 \x0300,01List: \x0304,01Feb 20th \x0309,01 \x0300,01Search: \x0304,01ON '
    '\x0309,01 \x0300,01Mode: \x0304,01Servers Only \x0309,01'
)

DISCO_PAL = (
    '\x0309,01\x0314,01 \x0304,01Type:\x0309,01 @disco_pal \x0304,01For My List '
    'Of:\x0309,01 475,128 \x0304,01Files \x0314,01 \x0304,01Slots:\x0309,01 4/4 '
    '\x0314,01 \x0304,01Queued:\x0309,01 0 \x0314,01 \x0304,01Speed:\x0309,01 0cps '
    '\x0314,01 \x0304,01Next: \x0309,01NOW \x0314,01 \x0304,01Served:\x0309,01 320,664 '
    '\x0314,01 \x0304,01List: \x0309,01Aug 19th \x0314,01 \x0304,01Search: '
    '\x0309,01OFF \x0314,01 \x0304,01Mode: \x0309,01Servers Only \x0314,01'
)

MDNIGHT = (
    '\x0304,00\x0314,00 \x0312,00Type:\x0304,00 @Mdnight \x0312,00For My List '
    'Of:\x0304,00 242,558 \x0312,00Files \x0314,00 \x0312,00Slots:\x0304,00 3/3 '
    '\x0314,00 \x0312,00Queued:\x0304,00 0 \x0314,00 \x0312,00Speed:\x0304,00 0cps '
    '\x0314,00 \x0312,00Next: \x0304,00NOW \x0314,00 \x0312,00Served:\x0304,00 104,598 '
    '\x0314,00 \x0312,00List: \x0304,00Aug 13th \x0314,00 \x0312,00Search: '
    '\x0304,00OFF \x0314,00 \x0312,00Mode: \x0304,00Normal \x0314,00'
)

RT_AU = (
    '\x0313,01\x0315,01\x95 \x0308,01Type:\x0313,01 @rt-au \x0308,01For My List '
    'Of:\x0313,01 82,388 \x0308,01Files \x0315,01\x95 \x0308,01Slots:\x0313,01 5/5 '
    '\x0315,01\x95 \x0308,01Queued:\x0313,01 0 \x0315,01\x95 \x0308,01Speed:\x0313,01 0cps '
    '\x0315,01\x95 \x0308,01Next: \x0313,01NOW \x0315,01\x95 \x0308,01Served:\x0313,01 2,139 '
    '\x0315,01\x95 \x0308,01List: \x0313,01Oct 1st \x0315,01\x95 \x0308,01Search: '
    '\x0313,01ON \x0315,01\x95 \x0308,01Mode: \x0313,01Normal \x0315,01\x95'
)

OUTLOOK = (
    '\x0313,15 \x036\x031\x02 \x02\x03\x030,1 For \x038My '
    'List(\x0313\x02\x0236342\x030files:\x0313\x02\x02345004\x030MB) and \x038DCC Status\x030, '
    'type \x038@outlook \x030and \x038@outlook-stats\x030. [(\x0313\x02\x020\x030/\x02\x025) '
    'Slots (\x0313\x02\x020\x030/\x02\x0295) Ques Taken] [Next Send: \x0313\x02\x02Open Slot '
    'Ready\x030] [CPS in Use: \x0313\x02\x020\x030] [Highest Cps Record: '
    '\x0313\x02\x026557\x030 by \x0313tempest\x030] [Total File Served: \x0313\x02\x0222805\x030] '
    '\x03\x031,15\x02 \x02\x036\x0313 \x03'
)

TESTBOT = (
    '\x0315,01\x0304,01\xae \x0314,01Type:\x0315,01 @TestBot \x0314,01For My List '
    'Of:\x0315,01 47,418 \x0314,01Files (\x0315,011.23TB\x0314,01) \x0304,01\xae '
    '\x0314,01Slots:\x0315,01 2/2 \x0304,01\xae \x0314,01Queued:\x0315,01 0 \x0304,01\xae '
    '\x0314,01Speed: (\x0315,010k/s / \x0315,0146.5MB/s\x0314,01) \x0304,01\xae '
    '\x0314,01Sent:\x0315,01 317,430 \x0304,01\xae \x0314,01List: \x0315,01Aug 27th '
    '\x0304,01\xae \x0314,01Search: \x0315,01OFF \x0304,01\xae \x0314,01Mode: '
    '\x0315,01Normal \x0304,01\xae'
)

RENOTA = (
    '\x0304,00Feel free to take what you need.\x0314,00 \x0312,00Type:\x0304,00 '
    '@Renota \x0312,00For My List Of:\x0304,00 534,636 \x0312,00Files \x0314,00 '
    '\x0312,00Slots:\x0304,00 5/5 \x0314,00 \x0312,00Queued:\x0304,00 0 \x0314,00 '
    '\x0312,00Speed:\x0304,00 0cps \x0314,00 \x0312,00Next: \x0304,00NOW \x0314,00 '
    '\x0312,00Served:\x0304,00 158,621 \x0314,00 \x0312,00List: \x0304,00Oct 18th '
    '\x0314,00 \x0312,00Search: \x0304,00ON \x0314,00 \x0312,00Mode: \x0304,00Normal '
    '\x0314,00'
)

QUIRKZ = (
    '\x0315,01\x0307,01* \x0309,01Type:\x0315,01 @quirkz \x0309,01For My List '
    'Of:\x0315,01 285,139 \x0309,01Files \x0307,01* \x0309,01Slots:\x0315,01 4/4 '
    '\x0307,01* \x0309,01Queued:\x0315,01 0 \x0307,01* \x0309,01Speed:\x0315,01 0cps '
    '\x0307,01* \x0309,01Next: \x0315,01NOW \x0307,01* \x0309,01Served:\x0315,01 '
    '2,102,238 \x0307,01* \x0309,01List: \x0315,01Jul 22nd \x0307,01* '
    '\x0309,01Search: \x0315,01ON \x0307,01* \x0309,01Mode: \x0315,01Normal \x0307,01*'
)

VA45TIDE = (
    '\x0309,01Wsm_Mlm For President\x0313,01\xae \x0304,01Type:\x0309,01 @va45tide- '
    '\x0304,01For My List Of:\x0309,01 95,747 \x0304,01Files \x0313,01\xae '
    '\x0304,01Slots:\x0309,01 8/8 \x0313,01\xae \x0304,01Queued:\x0309,01 0 \x0313,01\xae '
    '\x0304,01Speed:\x0309,01 0cps \x0313,01\xae \x0304,01Next: \x0309,01NOW \x0313,01\xae '
    '\x0304,01Served:\x0309,01 54,073 \x0313,01\xae \x0304,01List: \x0309,01Aug 17th '
    '\x0313,01\xae \x0304,01Search: \x0309,01ON \x0313,01\xae \x0304,01Mode: \x0309,01Normal '
    '\x0313,01\xae'
)

VALOGG = (
    '\x0308,01\x0300,01~ \x0307,01Type:\x0308,01 @ValOgg \x0307,01For My List '
    'Of:\x0308,01 442,439 \x0307,01Files \x0300,01~ \x0307,01Slots:\x0308,01 5/5 '
    '\x0300,01~ \x0307,01Queued:\x0308,01 0 \x0300,01~ \x0307,01Speed:\x0308,01 0cps '
    '\x0300,01~ \x0307,01Next: \x0308,01NOW \x0300,01~ \x0307,01Served:\x0308,01 '
    '601,278 \x0300,01~ \x0307,01List: \x0308,01Aug 28th \x0300,01~ '
    '\x0307,01Search: \x0308,01ON \x0300,01~ \x0307,01Mode: \x0308,01Servers '
    'Priority \x0300,01~'
)

VALOGG_RAR = (
    '\x0311,01 Type\x0308,01 @ValOgg^ \x0311,01to get my list of \x0308,0128,341 '
    '(972.23 GB) \x0311,01RAR folders \x0313,01\xbb\x0311,01 Available slots: '
    '\x0308,015/5 \x0313,01\xbb\x0311,01 Folders in queue: \x0308,010 \x0313,01\xbb\x0311,01 '
    'Folders sent: \x0308,01733 \x0313,01\xbb\x0311,01 Transfer speed: \x0308,010 '
    'KB/s \x0313,01\xbb\x0311,01 Updated list: \x0308,0128/Aug/26 \x0313,01\xbb\x0313,01 '
    '[ \x0308,01mx.rarserver v1.0.6\x0313,01 ] \x03'
)

ECHOSONIC = (
    '\x0300,07NOUVEAUT ... Indefendable Sans Trace.... tu cherches de '
    'la music, Karok, film, srie il a tout sa dans ma liste '
    ':-)\x0314,07 \x0312,07Type:\x0300,07 @Echosonic \x0312,07For My List '
    'Of:\x0300,07 718,005 \x0312,07Files \x0314,07 \x0312,07Slots:\x0300,07 25/25 '
    '\x0314,07 \x0312,07Queued:\x0300,07 9 \x0314,07 \x0312,07Speed:\x0300,07 0cps '
    '\x0314,07 \x0312,07Next: \x0300,07NOW \x0314,07 \x0312,07Served:\x0300,07 16,399 '
    '\x0314,07 \x0312,07List:'
)

ECHOSONIC_TAIL = (
    '\x0312,7\x0300,07Aug 25th \x0314,07 \x0312,07Search: \x0300,07ON \x0314,07 '
    '\x0312,07Mode: \x0300,07Normal \x0314,07'
)

# b0lt was in the very first capture and this file did not have it, because the
# sample was built by keeping the lines that PARSED - so the one bot whose
# wording defeated the parser was filtered out by the parser it defeats. It
# turned up when a second capture, from three channels these fixtures had never
# seen, was replayed and the senders that registered NOTHING were read by hand.
B0LT = (
    ' Type: @b0lt For My List Of 116,828 Files (1.82TB) Date: May 4th '
    '\xa4 Slots: 2/2 Queued: 0 Speed: 0KB/s (Avg: 6.52MB/s) Search: ON '
    'Mode: Normal \xa4 Served: 1,489,107 Since: Sep 2006 '
)

# QNet Advanced DCC File Server, from the same second capture. Deliberately not
# supported - see TheShapesThisDoesNotRead below.
QNET = (
    '>-< QNet Advanced DCC File Server >-< @relayop-audio @relayop-movies '
    '@relayop-tv for access >-< @relayop-help for info and options >-< '
    'Sharing 2.72TB of stuff! >-<'
)

ALL_CAPTURED = [
    ('[rigserv]', RIGSERV),
    ('`Glider', GLIDER),
    ('AvenIo', AVENIO),
    ('aditerra', ADITERRA),
    ('Wren', WREN),
    ('Kelta', KELTA),
    ('BigRig', BIGRIG),
    ('cobrrt', COBRRT),
    ('Zkx', ZKX),
    ('Den', DEN),
    ('Q_R_Q', Q_R_Q),
    ('DCCore', DCCORE),
    ('DCCoreWeb', DCCOREWEB),
    ('DCCoreWin', DCCOREWIN),
    ('Deepcove', DEEPCOVE),
    ('DJPhantom', DJPHANTOM),
    ('fallback77', FALLBACK77),
    ('IceWolfzzz', ICEWOLFZZZ),
    ('AudioVault', AUDIOVAULT),
    ('PhoneZone', PHONEZONE),
    ('Teravista', TERAVISTA),
    ('Oakwood', OAKWOOD),
    ('Northvale', NORTHVALE),
    ('disco_pal', DISCO_PAL),
    ('Mdnight', MDNIGHT),
    ('rt-au', RT_AU),
    ('outlook', OUTLOOK),
    ('TestBot', TESTBOT),
    ('Renota', RENOTA),
    ('quirkz', QUIRKZ),
    ('va45tide-', VA45TIDE),
    ('ValOgg', VALOGG),
    ('Echosonic', ECHOSONIC),
    ('b0lt', B0LT),
]

# The five bots whose advert does not fit one line: (nick, first, tail).
SPLIT_ADVERTS = [
    ('AvenIo', AVENIO, AVENIO_TAIL),
    ('DJPhantom', DJPHANTOM, DJPHANTOM_TAIL),
    ('fallback77', FALLBACK77, FALLBACK77_TAIL),
    ('Teravista', TERAVISTA, TERAVISTA_TAIL),
    ('Echosonic', ECHOSONIC, ECHOSONIC_TAIL),
]

# Bots serving a SECOND, separate list of RAR folders under a "^" trigger.
RAR_ADVERTS = [
    ('Zkx', ZKX_RAR),
    ('Teravista', TERAVISTA_RAR),
    ('ValOgg', VALOGG_RAR),
]

# The CTCP every OmenServe-family bot sends a few seconds after its advert.
# 27 of the 33 send one; the payload only, without its  wrapper.

RIGSERV_SLOTS = 'SLOTS 6 6 NOW 0 999 0 77018 10134698994329 0 1788010070 24063 OmenServe v2.71'
AVENIO_SLOTS = 'SLOTS 3 2 NOW 4 999 0 150623 10762353186689 0 1783537564 216072 OmeNServE v2.60'
ADITERRA_SLOTS = 'SLOTS 4 4 NOW 0 999 0 184437 1874763496619 0 1787931200 25861 OmenServe v2.73'
WREN_SLOTS = 'SLOTS 2 2 NOW 1 999 0 389136 1456773738862 0 1781954554 23166 OmeNServE v2.60'
BIGRIG_SLOTS = 'SLOTS 7 7 NOW 0 216 4788600 19527'
COBRRT_SLOTS = 'SLOTS 5 0 NOW 0 999 0 32119 133054739706 0 1787075324 11 OmeNServE v2.60'
ZKX_SLOTS = 'SLOTS 10 10 NOW 0 999 0 719041 3894929430520 0 1786359244 25511 OmenServe v2.73'
DEN_SLOTS = 'SLOTS 4 4 NOW 0 999 0 10965 80635194448 0 1788036122 23463 OmeNServE v2.60'
Q_R_Q_SLOTS = 'SLOTS 2 2 NOW 0 999 0 127312 1034287370931 0 1787959965 16873 OmeNServE v2.60'
DCCORE_SLOTS = 'SLOTS 3 3 NOW 0 999 0 61101 2021971462739 0 260125 272760943592 DCCore v1.10.0-RC3'
DCCOREWEB_SLOTS = 'SLOTS 3 3 NOW 0 999 0 61156 2023511524108 0 776 813728345 DCCore v1.10.0-RC4'
DCCOREWIN_SLOTS = 'SLOTS 3 3 NOW 0 999 0 36208 1329784322257 0 833 873601452 DCCore v1.10.0-RC4'
DEEPCOVE_SLOTS = 'SLOTS 6 6 NOW 0 999 0 288090 1531544257110 0 1777482551 24071 OmeNServE v2.50'
DJPHANTOM_SLOTS = 'SLOTS 20 20 NOW 0 999 0 101736 645115089833 0 1787664011 90972 OmenServe v2.71'
FALLBACK77_SLOTS = 'SLOTS 2 2 NOW 0 999 39659 14247378895149 1 1788018913 105071 OmeNServE v'
ICEWOLFZZZ_SLOTS = 'SLOTS 3 3 NOW 0 999 0 308738 2581183482308 0 1787571558 24075 OmeNServE v2.60'
AUDIOVAULT_SLOTS = 'SLOTS 2 2 NOW 0 999 0 313435 5270296864663 1 1767385035 24364 OmeNServE v2.60'
PHONEZONE_SLOTS = 'SLOTS 1 1 NOW 0 999 0 147636 13007675907209 0 1787914117 145300 OmeNServE v2.60'
TERAVISTA_SLOTS = 'SLOTS 5 5 NOW 0 999 0 96371 3199307179383 2 1785333363 99911 OmeNServE v2.60'
OAKWOOD_SLOTS = 'SLOTS 3 3 NOW 0 999 0 34842 237341768286 0 1785522128 334871 OmeNServE v2.60'
DISCO_PAL_SLOTS = 'SLOTS 4 4 NOW 0 999 0 475128 1913546153330 2 1787144654 23773 OmeNServE v2.60'
MDNIGHT_SLOTS = 'SLOTS 3 3 NOW 0 999 0 242558 2715497806325 0 1786660191 87967 OmenServe v2.71'
RT_AU_SLOTS = 'SLOTS 5 5 NOW 0 999 0 82388 589109188453 0 1759308895 35471 OmenServe v2.71'
OUTLOOK_SLOTS = 'SLOTS 5 5 NOW 0 95 6557000 36342'
TESTBOT_SLOTS = 'SLOTS 2 2 NOW 0 999 0 47418 1363377847376 0 1787833259 24061 OmeNServE v2.60'
VA45TIDE_SLOTS = 'SLOTS 8 8 NOW 0 999 0 95747 26457270004846 0 1786926708 24364 OmeNServE v2.60'
ECHOSONIC_SLOTS = 'SLOTS 25 25 NOW 9 999 0 718005 6760435525369 0 1787698644 270671 OmeNServE v2.60'

ALL_SLOTS = [
    ('[rigserv]', RIGSERV_SLOTS),
    ('AvenIo', AVENIO_SLOTS),
    ('aditerra', ADITERRA_SLOTS),
    ('Wren', WREN_SLOTS),
    ('BigRig', BIGRIG_SLOTS),
    ('cobrrt', COBRRT_SLOTS),
    ('Zkx', ZKX_SLOTS),
    ('Den', DEN_SLOTS),
    ('Q_R_Q', Q_R_Q_SLOTS),
    ('DCCore', DCCORE_SLOTS),
    ('DCCoreWeb', DCCOREWEB_SLOTS),
    ('DCCoreWin', DCCOREWIN_SLOTS),
    ('Deepcove', DEEPCOVE_SLOTS),
    ('DJPhantom', DJPHANTOM_SLOTS),
    ('fallback77', FALLBACK77_SLOTS),
    ('IceWolfzzz', ICEWOLFZZZ_SLOTS),
    ('AudioVault', AUDIOVAULT_SLOTS),
    ('PhoneZone', PHONEZONE_SLOTS),
    ('Teravista', TERAVISTA_SLOTS),
    ('Oakwood', OAKWOOD_SLOTS),
    ('disco_pal', DISCO_PAL_SLOTS),
    ('Mdnight', MDNIGHT_SLOTS),
    ('rt-au', RT_AU_SLOTS),
    ('outlook', OUTLOOK_SLOTS),
    ('TestBot', TESTBOT_SLOTS),
    ('va45tide-', VA45TIDE_SLOTS),
    ('Echosonic', ECHOSONIC_SLOTS),
]


class CaptureTestCase(DCCoreTestCase):
    """The registry and the continuation buffer are process-wide. A test that
    leaves either populated changes the next one."""

    def setUp(self):
        super().setUp()
        runtime.known_bots.clear()
        irc._advert_tails.clear()
        self.addCleanup(runtime.known_bots.clear)
        self.addCleanup(irc._advert_tails.clear)
        runtime.known_bots_flushed_at = T0      # suppress disk writes

    def capture(self, nick, text, now=T0, channel="#DCCore-Test"):
        irc._capture_channel_advert(nick, channel, text, now=now)

    def entry(self, nick):
        return runtime.known_bots[nick.lower()]


class ReadingRealAdverts(unittest.TestCase):
    """The OmenServe wording - 28 of the 33 bots."""

    def test_a_typical_advert(self):
        advert = irc.parse_channel_advert(ZKX)

        self.assertEqual(advert["nick"], "Zkx")
        self.assertEqual(advert["files"], 719041)
        self.assertEqual(advert["list_date"], "Aug 10th")

    def test_decoration_between_every_field(self):
        """Five different separator characters across five bots. Keying on
        anything but the labels would need a rule per bot."""
        for text, nick, files in ((Q_R_Q, "Q_R_Q", 127312),
                                  (WREN, "Wren", 389136),
                                  (TESTBOT, "TestBot", 47418),
                                  (QUIRKZ, "quirkz", 285139),
                                  (AVENIO, "AvenIo", 150623)):
            with self.subTest(nick=nick):
                advert = irc.parse_channel_advert(text)
                self.assertEqual(advert["nick"], nick)
                self.assertEqual(advert["files"], files)

    def test_separators_the_daemon_never_sees(self):
        """Several bots decorate with bytes that are not valid UTF-8, and
        irc.py reads the socket with errors="ignore" - so the separators are
        gone before the parser is ever called."""
        for text, nick in ((ICEWOLFZZZ, "IceWolfzzz"), (KELTA, "Kelta"),
                           (RENOTA, "Renota"), (MDNIGHT, "Mdnight"),
                           (NORTHVALE, "Northvale")):
            with self.subTest(nick=nick):
                self.assertEqual(irc.parse_channel_advert(text)["nick"], nick)

    def test_a_control_character_between_the_fields(self):
        """The "\\x95" separators are a cp1252 bullet that arrived
        double-encoded. That makes them valid UTF-8, so unlike the ones above
        they survive - and strip_control_codes() does not remove them either,
        since it strips mIRC formatting rather than C1."""
        for text, nick in ((ZKX, "Zkx"), (DJPHANTOM, "DJPhantom"),
                           (FALLBACK77, "fallback77"), (AUDIOVAULT, "AudioVault")):
            with self.subTest(nick=nick):
                self.assertEqual(irc.parse_channel_advert(text)["nick"], nick)

    def test_no_separators_at_all(self):
        """disco_pal uses plain single spacing, so "Files Slots:" runs
        together with nothing between the fields."""
        advert = irc.parse_channel_advert(DISCO_PAL)

        self.assertEqual(advert["nick"], "disco_pal")
        self.assertEqual(advert["files"], 475128)
        self.assertEqual(advert["list_date"], "Aug 19th")

    def test_nicks_that_are_not_words(self):
        """@[rigserv] and @`Glider - a word-characters-only pattern misses
        both entirely."""
        self.assertEqual(irc.parse_channel_advert(RIGSERV)["nick"], "[rigserv]")
        self.assertEqual(irc.parse_channel_advert(GLIDER)["nick"], "`Glider")

    def test_preamble_before_the_advert(self):
        """Bots open with an album count, a sentence of French, a note about an
        OP. The advert is not the whole line and cannot be anchored to its
        start."""
        for text, nick in ((RIGSERV, "[rigserv]"), (DJPHANTOM, "DJPhantom"),
                           (FALLBACK77, "fallback77"), (AUDIOVAULT, "AudioVault"),
                           (RENOTA, "Renota")):
            with self.subTest(nick=nick):
                self.assertEqual(irc.parse_channel_advert(text)["nick"], nick)

    def test_our_own_wording_for_the_date_and_size(self):
        """DCCore says "created Aug 27th" where the rest say "List: Aug 27th",
        and puts a size in "Files (1.21TB)"."""
        advert = irc.parse_channel_advert(DCCOREWIN)

        self.assertEqual(advert["nick"], "DCCoreWin")
        self.assertEqual(advert["files"], 36208)
        self.assertEqual(advert["list_date"], "Aug 27th")
        self.assertEqual(advert["list_size"], "1.21TB")

    def test_one_other_bot_publishes_a_size_the_same_way(self):
        """TestBot runs something that writes "Files (1.23TB)" too. Pinned
        because it is the only non-DCCore bot in the sample that does, so it is
        the one that would break if the size pattern were narrowed to our own
        wording."""
        advert = irc.parse_channel_advert(TESTBOT)

        self.assertEqual(advert["files"], 47418)
        self.assertEqual(advert["list_size"], "1.23TB")

    def test_dates_from_other_months(self):
        """Renota's list is from last October, AudioVault's from January,
        Northvale's from February. Stale lists are the whole reason for reading
        the date, so a pattern that only matched the current month would read
        as "everything is current"."""
        self.assertEqual(irc.parse_channel_advert(RENOTA)["list_date"], "Oct 18th")
        self.assertEqual(irc.parse_channel_advert(AUDIOVAULT)["list_date"], "Jan 2nd")
        self.assertEqual(irc.parse_channel_advert(NORTHVALE)["list_date"], "Feb 20th")
        self.assertEqual(irc.parse_channel_advert(WREN)["list_date"], "Jun 20th")

    def test_a_missing_size_is_absent_not_zero(self):
        """The same rule parse_search_header() follows: absent means "did not
        say". A freshness check that treats silence as a value invents one."""
        for text in (ZKX, Q_R_Q, RIGSERV, DJPHANTOM, WREN, DISCO_PAL):
            with self.subTest(text=text[:24]):
                self.assertNotIn("list_size", irc.parse_channel_advert(text))

    def test_colour_codes_are_stripped_before_the_numbers_are_read(self):
        """The one case here that is not verbatim, and it is marked as such:
        the observer stripped mIRC colour before writing the sample.

        It cannot be left untested, because a colour code carries DIGITS. mIRC
        writes it as \\x03 followed by up to two of them, and bots put one
        immediately before the figure they want coloured - so the count pattern
        would read the colour number as part of the count and return a file
        count off by a factor of ten thousand.
        """
        coloured = ZKX.replace("719,041", "\x0304719,041\x03")

        self.assertEqual(irc.parse_channel_advert(coloured)["files"], 719041)

    def test_every_captured_advert_parses(self):
        """The sample is the specification. If any real advert stops parsing,
        that is the regression."""
        for nick, text in ALL_CAPTURED:
            with self.subTest(nick=nick):
                advert = irc.parse_channel_advert(text)
                self.assertIsNotNone(advert, "a real advert stopped parsing")
                self.assertEqual(advert["nick"], nick)
                self.assertGreater(advert["files"], 0)


class TheAdvertDoesNotAlwaysFitOneLine(CaptureTestCase):
    """IRC drops anything past 512 bytes, so five of the 33 bots send their
    advert as two PRIVMSGs and the break lands wherever they ran out of room.

    Echosonic's lands on the list date. Read line by line, its advert says the
    bot published no date at all - and "no date" is not a small error here, it
    is the entire freshness signal for that bot.
    """

    def test_the_first_line_alone_loses_the_date(self):
        """The defect, stated directly. This is what the daemon saw before."""
        self.assertNotIn("list_date", irc.parse_channel_advert(ECHOSONIC))

    def test_the_second_line_alone_is_not_an_advert(self):
        """It has no trigger and no count, so nothing can be done with it on
        its own - it has to be joined to what came before."""
        self.assertIsNone(irc.parse_channel_advert(ECHOSONIC_TAIL))

    def test_the_two_together_carry_the_date(self):
        self.capture("Echosonic", ECHOSONIC, now=T0)
        self.capture("Echosonic", ECHOSONIC_TAIL, now=T0 + 3)

        self.assertEqual(self.entry("Echosonic")["list_date"], "Aug 25th")

    def test_the_first_line_s_own_fields_are_not_lost_in_the_merge(self):
        self.capture("Echosonic", ECHOSONIC, now=T0)
        self.capture("Echosonic", ECHOSONIC_TAIL, now=T0 + 3)

        self.assertEqual(self.entry("Echosonic")["files"], 718005)

    def test_every_split_advert_in_the_sample(self):
        """All five, through the real capture path. The other four continue
        with only a "Mode:" field, so they lose nothing either way - but they
        are the evidence that the split is a habit of the software rather than
        one bot's quirk."""
        for nick, first, tail in SPLIT_ADVERTS:
            with self.subTest(nick=nick):
                runtime.known_bots.clear()
                irc._advert_tails.clear()
                self.capture(nick, first, now=T0)
                self.capture(nick, tail, now=T0 + 3)

                self.assertIn(nick.lower(), runtime.known_bots)
                self.assertGreater(self.entry(nick)["files"], 0)

    def test_a_tail_that_arrives_too_late_is_not_joined(self):
        """Every split in the sample arrived within three seconds. A line
        minutes later is the bot talking, not the rest of its advert."""
        self.capture("Echosonic", ECHOSONIC, now=T0)
        self.capture("Echosonic", ECHOSONIC_TAIL,
                     now=T0 + irc.ADVERT_CONTINUATION_SECONDS + 1)

        self.assertNotIn("list_date", self.entry("Echosonic"))

    def test_a_tail_from_someone_else_is_not_joined(self):
        """The buffer is keyed by sender and written only after the sender
        check has passed, so a stranger cannot feed a tail to a bot's advert."""
        self.capture("Echosonic", ECHOSONIC, now=T0)
        self.capture("randomuser", ECHOSONIC_TAIL, now=T0 + 3)

        self.assertNotIn("list_date", self.entry("Echosonic"))
        self.assertNotIn("randomuser", runtime.known_bots)

    def test_ordinary_chatter_after_an_advert_changes_nothing(self):
        """A complete advert followed by the bot saying something. The merge
        re-reads the whole thing, so the fields simply come out the same."""
        self.capture("Zkx", ZKX, now=T0)
        self.capture("Zkx", "brb, restarting", now=T0 + 2)

        self.assertEqual(self.entry("Zkx")["files"], 719041)
        self.assertEqual(self.entry("Zkx")["list_date"], "Aug 10th")

    def test_a_later_line_cannot_overwrite_a_field_the_advert_already_gave(self):
        """The patterns take their FIRST match, so a bot chatting about some
        other date after a complete advert cannot move its list date. Echosonic
        works only because its own line 1 ends ON the label with nothing after
        it."""
        self.capture("Zkx", ZKX, now=T0)
        self.capture("Zkx", "my next List: Dec 25th maybe", now=T0 + 2)

        self.assertEqual(self.entry("Zkx")["list_date"], "Aug 10th")

    def test_a_talkative_bot_cannot_walk_the_window_forward(self):
        """The buffer keeps the ORIGINAL timestamp when it grows. Without that,
        a bot that speaks every five seconds would keep its advert stitchable
        forever and accumulate the whole conversation.

        Asserted on the buffer rather than on the merged date, because a merged
        date cannot see this: anything appended after a truncated "List:" sits
        between the label and the value and stops that date resolving anyway.
        An earlier version of this test checked the date and passed for exactly
        that reason, with the window never involved.
        """
        self.capture("Echosonic", ECHOSONIC, now=T0)
        for step in range(1, 4):
            self.capture("Echosonic", "still here", now=T0 + step * 5)

        self.capture("Echosonic", "and again",
                     now=T0 + irc.ADVERT_CONTINUATION_SECONDS + 5)

        self.assertEqual(irc._advert_tails, {},
                         "the advert stayed stitchable past its window")

    def test_chatter_between_the_two_halves_costs_the_continuation(self):
        """A real limit, written down rather than papered over.

        The stitch is a plain join, so a line that arrives between the two
        halves lands where the value should be and the field does not resolve.
        Every split in the sample arrives back to back, 0 to 3 seconds apart
        with nothing in between, so this has no cost today - but it is what
        would happen, and it is why the join is not cleverer than it looks.
        """
        self.capture("Echosonic", ECHOSONIC, now=T0)
        self.capture("Echosonic", "one moment", now=T0 + 1)
        self.capture("Echosonic", ECHOSONIC_TAIL, now=T0 + 2)

        self.assertNotIn("list_date", self.entry("Echosonic"))

    def test_a_ctcp_between_the_two_halves_does_not_break_the_stitch(self):
        """Every bot sends a CTCP SLOTS a few seconds after its advert. In the
        sample it always lands after the second half, but nothing guarantees
        that order - and joined in, its payload would sit exactly where the
        missing value goes, which is the chatter case above with the bot's own
        protocol traffic as the chatter. A CTCP is not channel text, so it
        never reaches the stitch at all.
        """
        self.capture("Echosonic", ECHOSONIC, now=T0)
        self.capture("Echosonic",
                     "SLOTS 25 25 NOW 9 999 0 718005 6760435525369",
                     now=T0 + 1)
        self.capture("Echosonic", ECHOSONIC_TAIL, now=T0 + 2)

        self.assertEqual(self.entry("Echosonic")["list_date"], "Aug 25th")

    def test_a_third_line_can_still_be_joined(self):
        """Constructed, not captured - nothing in the sample splits three ways.
        The buffer keeps the stitched text, so an advert broken into more than
        two pieces costs nothing extra."""
        head, _, rest = ZKX.partition("Slots:")
        middle, _, tail = rest.partition("List:")

        self.capture("Zkx", head, now=T0)
        self.capture("Zkx", "Slots:" + middle, now=T0 + 1)
        self.capture("Zkx", "List:" + tail, now=T0 + 2)

        self.assertEqual(self.entry("Zkx")["list_date"], "Aug 10th")

    def test_the_stitched_text_cannot_grow_without_bound(self):
        """Asserted on the buffer, for the same reason as the window test above:
        a long enough line makes the date unresolvable on its own, so the date
        cannot tell whether the cap did anything."""
        self.capture("Echosonic", ECHOSONIC, now=T0)

        self.capture("Echosonic", "x" * irc.ADVERT_MAX_STITCHED_CHARS, now=T0 + 1)

        self.assertEqual(irc._advert_tails, {},
                         "an oversized line was kept for further stitching")

    def test_the_buffer_does_not_keep_growing(self):
        """One entry per bot, dropped once it is too old to be continued."""
        for index in range(50):
            self.capture("Zkx", ZKX, now=T0 + index * 60)

        self.assertLessEqual(len(irc._advert_tails), 1)


class SPQRSpeaksDifferently(CaptureTestCase):
    """BigRig and outlook run SPQR, which shares not one phrase with the
    OmenServe wording - no colon after "Type", no "Of:", and the count and size
    inside one parenthesis. Both were invisible to this daemon until it was
    taught the sentence.
    """

    def test_it_parses(self):
        advert = irc.parse_channel_advert(BIGRIG)

        self.assertEqual(advert["nick"], "BigRig")
        self.assertEqual(advert["files"], 19527)
        self.assertEqual(advert["list_size"], "163812MB")

    def test_the_second_one_too(self):
        advert = irc.parse_channel_advert(OUTLOOK)

        self.assertEqual(advert["nick"], "outlook")
        self.assertEqual(advert["files"], 36342)

    def test_the_stats_trigger_is_not_mistaken_for_the_nick(self):
        """The sentence names two triggers - "type @BigRig and
        @BigRig-stats" - and only the first is the bot."""
        self.assertEqual(irc.parse_channel_advert(BIGRIG)["nick"], "BigRig")

    def test_it_publishes_no_date(self):
        """Which is exactly why absent has to mean "did not say": SPQR bots
        would otherwise all read as infinitely stale."""
        self.assertNotIn("list_date", irc.parse_channel_advert(BIGRIG))

    def test_it_reaches_the_registry(self):
        self.capture("BigRig", BIGRIG)

        self.assertEqual(self.entry("BigRig")["files"], 19527)


class ASecondListOfRarFolders(CaptureTestCase):
    """Three bots serve a SECOND list beside their loose files: RAR folders,
    advertised in their own message under a "^"-suffixed trigger.

    Zkx publishes 719,041 loose files and 39,454 RAR folders. They are two
    different lists with two different triggers, and collapsing them would
    report a number that is neither.
    """

    def test_it_parses(self):
        advert = irc.parse_channel_advert(ZKX_RAR)

        self.assertEqual(advert["nick"], "Zkx")
        self.assertEqual(advert["rar_folders"], 39454)
        self.assertEqual(advert["rar_size"], "5.48TB")
        self.assertEqual(advert["rar_trigger"], "Zkx^")

    def test_the_trigger_suffix_is_not_part_of_the_nick(self):
        """"Zkx^" is what you type at it; "Zkx" is who sends it. Keeping the
        "^" would fail the sender check and drop every one of these."""
        self.capture("Zkx", ZKX_RAR)

        self.assertIn("zkx", runtime.known_bots)
        self.assertEqual(self.entry("Zkx")["rar_trigger"], "Zkx^")

    def test_a_bot_keeps_both_lists(self):
        self.capture("Zkx", ZKX, now=T0)
        self.capture("Zkx", ZKX_RAR, now=T0 + 60)

        entry = self.entry("Zkx")
        self.assertEqual(entry["files"], 719041)
        self.assertEqual(entry["list_date"], "Aug 10th")
        self.assertEqual(entry["rar_folders"], 39454)

    def test_neither_advert_overwrites_the_other(self):
        """Order must not matter. The RAR advert carries no file count and the
        file advert carries no folder count, so whichever lands second must
        leave the other alone."""
        self.capture("Zkx", ZKX_RAR, now=T0)
        self.capture("Zkx", ZKX, now=T0 + 60)

        entry = self.entry("Zkx")
        self.assertEqual(entry["files"], 719041)
        self.assertEqual(entry["rar_folders"], 39454)

    def test_all_three_in_the_sample(self):
        for nick, text in RAR_ADVERTS:
            with self.subTest(nick=nick):
                advert = irc.parse_channel_advert(text)
                self.assertEqual(advert["nick"], nick)
                self.assertEqual(advert["rar_trigger"], nick + "^")
                self.assertGreater(advert["rar_folders"], 0)


class TheExactSizeComesFromTheCtcp(CaptureTestCase):
    """Every OmenServe-family bot follows its advert with a CTCP SLOTS line,
    and it is the only place the exact size of a library is published. We have
    been sending ours since the daemon existed and never read anyone else's.

    READING IT BY POSITION DOES NOT WORK

    Three layouts in one channel. fallback77's line is one field shorter than
    everyone else's, so the index that holds 719,041 files for Zkx holds
    14,247,378,895,149 for fallback77 - which as a file count is fourteen
    trillion files, and would have gone onto the dashboard as one.

    So the count the bot's own advert already gave is used to find the field,
    and the size is whatever sits beside it. The layout never has to be known,
    only self-consistent.
    """

    def test_the_common_layout(self):
        found = irc.parse_advert_slots(ZKX_SLOTS, 719041)

        self.assertEqual(found["list_bytes"], 3894929430520)      # 3.54 TB
        self.assertEqual(found["software"], "OmenServe v2.73")

    def test_the_layout_that_is_one_field_short(self):
        """fallback77, and the whole reason nothing here counts fields. Its
        size sits where every other bot puts its file count."""
        found = irc.parse_advert_slots(FALLBACK77_SLOTS, 39659)

        self.assertEqual(found["list_bytes"], 14247378895149)     # 12.96 TB

    def test_the_number_a_positional_read_would_have_believed(self):
        """The defect stated as a value: 14 trillion is what index 7 holds on
        that line, and it is not a file count."""
        fields = FALLBACK77_SLOTS.split()

        self.assertEqual(fields[7], "14247378895149")
        self.assertEqual(fields[6], "39659", "the count is at 6, not 7")

    def test_spqr_publishes_no_size_and_none_is_invented(self):
        """BigRig's line ends at the file count. There is no field beside it,
        so there is no size - not a zero, and not whatever came before."""
        found = irc.parse_advert_slots(BIGRIG_SLOTS, 19527)

        self.assertNotIn("list_bytes", found)

    def test_understood_but_empty_is_not_the_same_as_unreadable(self):
        """SPQR's line agrees with the advert and carries nothing else, which
        is a fact about that bot. A line that does not agree is a fact about
        the line. Both mean "record nothing" and they are still not the same,
        so one is {} and the other is None."""
        self.assertEqual(irc.parse_advert_slots(BIGRIG_SLOTS, 19527), {})
        self.assertIsNone(irc.parse_advert_slots(BIGRIG_SLOTS, 999))

    def test_a_line_that_disagrees_with_the_advert_is_refused(self):
        """If the count is not in the line at all, nothing in it can be located
        with confidence, so none of it is used."""
        self.assertIsNone(irc.parse_advert_slots(ZKX_SLOTS, 12345))

    def test_an_ambiguous_count_is_refused(self):
        """Two fields carrying the same number means two candidate neighbours.
        A guess here is a wrong size on the dashboard rather than a missing
        one, so it is refused."""
        self.assertIsNone(irc.parse_advert_slots("SLOTS 7 7 NOW 0 999 0 7 12345", 7))

    def test_a_size_smaller_than_the_file_count_is_refused(self):
        """A library cannot hold fewer bytes than it holds files."""
        found = irc.parse_advert_slots(
            "SLOTS 3 3 NOW 0 999 0 500000 400 0 1 2 Thing v1", 500000)

        self.assertNotIn("list_bytes", found)

    def test_an_implausible_size_is_refused(self):
        """The ceiling is what stops a misread landing on the dashboard as a
        real number. The largest library in the captured channel is 24 TB."""
        huge = "SLOTS 3 3 NOW 0 999 0 500000 %d 0 1 2 Thing v1" % (10 ** 17)

        self.assertNotIn("list_bytes", irc.parse_advert_slots(huge, 500000))

    def test_a_bot_with_no_files_yet_reads_nothing(self):
        """A count of zero is not something to calibrate on. Three of the 27
        captured lines carry exactly one "0" field, so a bot whose list is
        still empty - a fresh install, which is every install once - would
        match it and read the field beside it as its library size. BigRig's
        line would make that 216 bytes.
        """
        self.assertIsNone(irc.parse_advert_slots(BIGRIG_SLOTS, 0))
        self.assertIsNone(irc.parse_advert_slots(BIGRIG_SLOTS, None))

    def test_a_file_offer_is_not_a_slots_line(self):
        """Serving bots send CTCPs other than SLOTS. fallback77 offers
        individual files to the channel between its adverts:

            MP3 House Party 3 (1994)-DVDRIp-AC3-Xvid-THC.avi

        Filenames carry numbers - years, bitrates, track counts - so without
        checking what kind of message this is first, a bot with 1,994 files
        would read the next token of a filename as its library size.
        """
        offer = "MP3 Best of 1994 320 kbps.mp3"

        self.assertIsNone(irc.parse_advert_slots(offer, 1994))

    def test_something_that_is_not_a_slots_line(self):
        self.assertIsNone(irc.parse_advert_slots("MP3 Some Song.mp3", 719041))
        self.assertIsNone(irc.parse_advert_slots("", 719041))

    def test_nothing_is_read_without_a_count_to_check_against(self):
        """The calibration IS the safety check, so a bot that has not
        advertised gets nothing read from its CTCP."""
        self.assertIsNone(irc.parse_advert_slots(ZKX_SLOTS, None))
        self.assertIsNone(irc.parse_advert_slots(ZKX_SLOTS, 0))

    def test_the_software_version_is_read_as_a_suffix(self):
        """Read as whatever trails the numbers rather than at an index, so the
        short layout and the truncated one both work."""
        self.assertEqual(
            irc.parse_advert_slots(DCCOREWIN_SLOTS, 36208)["software"],
            "DCCore v1.10.0-RC4")
        self.assertEqual(
            irc.parse_advert_slots(FALLBACK77_SLOTS, 39659)["software"],
            "OmeNServE v")

    def test_spqr_sends_no_version_and_none_is_invented(self):
        self.assertNotIn("software", irc.parse_advert_slots(BIGRIG_SLOTS, 19527))

    def test_a_bot_that_has_not_advertised_gets_nothing_recorded(self):
        """Through the capture path: a CTCP on its own registers nobody."""
        self.capture("Zkx", CTCP + ZKX_SLOTS + CTCP)

        self.assertEqual(runtime.known_bots, {})

    def test_the_advert_and_the_ctcp_together(self):
        self.capture("Zkx", ZKX, now=T0)
        self.capture("Zkx", CTCP + ZKX_SLOTS + CTCP, now=T0 + 5)

        entry = self.entry("Zkx")
        self.assertEqual(entry["files"], 719041)
        self.assertEqual(entry["list_date"], "Aug 10th")
        self.assertEqual(entry["list_bytes"], 3894929430520)
        self.assertEqual(entry["software"], "OmenServe v2.73")

    def test_a_stranger_cannot_resize_a_bot_s_library(self):
        """The CTCP is looked up by SENDER, so someone else's SLOTS line finds
        no entry of that bot's to write into."""
        self.capture("Zkx", ZKX, now=T0)
        forged = ZKX_SLOTS.replace("3894929430520", "9000000000000")
        self.capture("randomuser", CTCP + forged + CTCP, now=T0 + 5)

        self.assertNotIn("list_bytes", self.entry("Zkx"))

    def test_every_captured_slots_line_reads_or_refuses(self):
        """The whole sample. Every one of the 27 either yields a size that
        agrees with the bot's own advert, or yields none - never a number that
        is not a size."""
        for nick, text in ALL_CAPTURED:
            self.capture(nick, text)

        read = 0
        for nick, payload in ALL_SLOTS:
            with self.subTest(nick=nick):
                entry = runtime.known_bots.get(nick.lower())
                if not entry:
                    continue
                self.capture(nick, CTCP + payload + CTCP)
                size = entry.get("list_bytes")
                if size is None:
                    continue
                read += 1
                self.assertGreaterEqual(size, entry["files"])
                self.assertLess(size, irc.SLOTS_MAX_PLAUSIBLE_BYTES)

        self.assertGreaterEqual(read, 24,
                                "the calibration stopped finding the count")

    def test_the_exact_size_agrees_with_the_advertised_one(self):
        """Cross-check against the bots that publish a size in words as well.
        If the field beside the count were the wrong field, these would not
        line up to two decimal places."""
        for nick, advert, payload, expected in (
                ("DCCoreWin", DCCOREWIN, DCCOREWIN_SLOTS, "1.21TB"),
                ("DCCore", DCCORE, DCCORE_SLOTS, "1.84TB")):
            with self.subTest(nick=nick):
                runtime.known_bots.clear()
                self.capture(nick, advert, now=T0)
                self.capture(nick, CTCP + payload + CTCP, now=T0 + 5)

                entry = self.entry(nick)
                as_tb = entry["list_bytes"] / (1024.0 ** 4)
                self.assertEqual("%.2fTB" % as_tb, expected)
                self.assertEqual(entry["list_size"], expected)


class TheWordingVariesMoreThanOneChannelShows(CaptureTestCase):
    """b0lt, and how it was found.

    These fixtures came from one twenty-minute window in one channel, and the
    sample was assembled by keeping the lines that PARSED. That is a filter
    with an obvious hole in hindsight: a bot whose wording defeats the parser
    is invisible to a sample the parser selected. b0lt sat in the very first
    capture the whole time.

    It turned up when a second capture - three channels these fixtures had
    never seen - was replayed through the real capture path and the senders
    that registered NOTHING were read by hand. Everything else in that capture
    was a trivia game, a greeter, or people talking. b0lt was a file server
    with 116,828 files and nobody was listening to it.

    Two differences, both small, both fatal on their own:

      * "For My List Of 116,828 Files" - no colon after "Of"
      * "Date: May 4th" where the rest of the family says "List:"
    """

    def test_the_count_survives_a_missing_colon(self):
        advert = irc.parse_channel_advert(B0LT)

        self.assertIsNotNone(advert, "the whole advert was unreadable")
        self.assertEqual(advert["nick"], "b0lt")
        self.assertEqual(advert["files"], 116828)

    def test_a_third_way_of_writing_the_date(self):
        """"List:", "created", and now "Date:". The date is the entire
        freshness signal, so a label this family uses and the parser does not
        know reads as "never published one"."""
        self.assertEqual(irc.parse_channel_advert(B0LT)["list_date"], "May 4th")

    def test_it_publishes_a_size_as_well(self):
        self.assertEqual(irc.parse_channel_advert(B0LT)["list_size"], "1.82TB")

    def test_it_reaches_the_registry(self):
        self.capture("b0lt", B0LT)

        entry = self.entry("b0lt")
        self.assertEqual(entry["files"], 116828)
        self.assertEqual(entry["list_date"], "May 4th")

    def test_the_looser_count_still_needs_a_trigger(self):
        """Dropping the colon widens the pattern, so the half that keeps it
        honest is worth re-checking: a sentence about somebody's list is still
        not an advert."""
        self.assertIsNone(irc.parse_channel_advert(
            "he told me For My List Of 500 Files just wait"))


class TheShapesThisDoesNotRead(unittest.TestCase):
    """Recorded rather than forgotten.

    The second capture turned up one more file server, running software this
    parser has no answer for. It is written down here so the next person meets
    a decision rather than a silence.
    """

    def test_qnet_is_not_read(self):
        """QNet Advanced DCC File Server:

            >-< QNet Advanced DCC File Server >-< @relayop-audio
            @relayop-movies @relayop-tv for access >-< Sharing 2.72TB <

        It does not fit the registry as it stands, and not because of wording.
        It advertises THREE lists under three triggers, and publishes no file
        count for any of them - only a total size. Every entry here is keyed on
        one nick with one count, and the CTCP that carries exact bytes is
        calibrated against that count, so there is nothing to check its numbers
        against either.

        Supporting it means deciding what a bot with several lists and no
        counts looks like in the sidebar, which is a design question for #133
        rather than a pattern to widen. One bot in sixty-one, so the cost of
        waiting is knowing about one server and not listing it.
        """
        self.assertIsNone(irc.parse_channel_advert(QNET))

    def test_a_per_file_offer_is_still_not_a_list_advert(self):
        """A different bot boasts about its library in prose and offers one
        file at a time under "!" rather than "@". The numbers are there, in
        European notation, and they are not a list advert - the same call
        already made for fallback77's file offers."""
        self.assertIsNone(irc.parse_channel_advert(
            "  86.994  Songs in English, Spanish, Dutch, German, French, "
            "Italian, Russian, etc...  Also   3.665  Christmas Songs  &  "
            "12.902 Instrumentals! Type: !Melodia Matt Monro - No Puedo "
            "Quitar Mis Ojos De Ti.mp3 To Get This  3.17MB 3m28s MP3"))


class NotAnAdvert(unittest.TestCase):
    """Both halves are needed. Either alone is ordinary chatter."""

    def test_plain_conversation(self):
        self.assertIsNone(irc.parse_channel_advert("anyone got the new Slayer?"))

    def test_someone_telling_a_friend_what_to_type(self):
        self.assertIsNone(irc.parse_channel_advert("just type @Zkx and wait"))

    def test_a_count_with_no_trigger(self):
        self.assertIsNone(irc.parse_channel_advert("For My List Of: 500 Files"))

    def test_a_trigger_with_no_count(self):
        """An op answering a newcomer writes the trigger exactly as a bot
        would - and an advert is a bot describing its own list, so without a
        count there is nothing to describe."""
        self.assertIsNone(irc.parse_channel_advert(
            "welcome - Type: @Zkx in the channel and he will send you his list"))

    def test_a_bot_quoted_in_conversation(self):
        self.assertIsNone(irc.parse_channel_advert(
            "he said Type: @AudioVault but it never answers me"))

    def test_a_search_result_line(self):
        self.assertIsNone(irc.parse_channel_advert(
            "!Zkx Slayer - Angel Of Death.mp3  ::INFO:: 4.6MB"))

    def test_a_file_offer_from_a_serving_bot(self):
        """fallback77 advertises individual files between its list adverts.
        Same bot, same channel, not a list advert."""
        self.assertIsNone(irc.parse_channel_advert(
            " Maybe u want THiS FiLE.... Type: !fallback77 House Party 3 "
            "(1994)-DVDRIp-AC3-Xvid-THC.avi To Get This File"))

    def test_nothing_at_all(self):
        self.assertIsNone(irc.parse_channel_advert(""))
        self.assertIsNone(irc.parse_channel_advert(None))


class TheSenderIsTheAuthority(CaptureTestCase):
    """"Type: @Someone" is characters in a message, and any user can type them.

    The registry exists to decide whether a list we hold is current, so a
    poisoned entry means showing a list as that bot's current one when it was
    never theirs.
    """

    def test_an_advert_matching_its_sender_is_recorded(self):
        self.capture("Zkx", ZKX)

        self.assertEqual(self.entry("Zkx")["files"], 719041)

    def test_a_user_claiming_to_be_a_bot_is_ignored(self):
        self.capture("randomuser", ZKX)

        self.assertEqual(runtime.known_bots, {},
                         "a user impersonated a bot and it was believed")

    def test_an_impersonator_leaves_nothing_behind_to_continue(self):
        """A rejected advert must not seed the continuation buffer either, or
        the check could be walked around one line at a time."""
        self.capture("randomuser", ECHOSONIC, now=T0)
        self.capture("randomuser", ECHOSONIC_TAIL, now=T0 + 3)

        self.assertEqual(runtime.known_bots, {})

    def test_the_match_is_case_insensitive(self):
        """IRC nicks are case-insensitive, and a server may echo a different
        case than the bot uses inside its own advert."""
        self.capture("ZKX", ZKX)

        self.assertIn("zkx", runtime.known_bots)

    def test_the_sender_supplies_the_casing_too(self):
        """The advert text is a template the bot wrote once; the sender nick is
        what the server says it is called right now. That is the one somebody
        would type at it, so that is the one worth storing."""
        self.capture("ZKX", ZKX)      # the text says "Zkx"

        self.assertEqual(self.entry("Zkx")["nick"], "ZKX")

    def test_a_private_message_is_not_a_channel_advert(self):
        """Adverts are broadcast. A direct message shaped like one is somebody
        trying something."""
        self.capture("Zkx", ZKX, channel="DCCoreWin")

        self.assertEqual(runtime.known_bots, {})

    def test_a_ctcp_is_not_channel_text(self):
        """Bots send a CTCP SLOTS seconds after their advert. It is not an
        advert and must not be treated as one - nor as a continuation of the
        advert it follows."""
        self.capture("Zkx", ZKX, now=T0)
        self.capture("Zkx", "\x01SLOTS 10 10 NOW 0 999 0 719041 3894929430520\x01",
                     now=T0 + 5)

        self.assertEqual(self.entry("Zkx")["files"], 719041)

    def test_ordinary_chatter_registers_nobody(self):
        self.capture("Zkx", "back in 10")

        self.assertEqual(runtime.known_bots, {})

    def test_a_later_advert_updates_rather_than_duplicates(self):
        self.capture("Zkx", ZKX, now=T0)
        self.capture("Zkx", ZKX.replace("719,041", "720,000"), now=T0 + 300)

        self.assertEqual(len(runtime.known_bots), 1)
        self.assertEqual(self.entry("Zkx")["files"], 720000)

    def test_a_field_a_bot_stops_publishing_is_not_forgotten(self):
        """Adverts get truncated. Dropping a date because one advert arrived
        without it would flip a bot to "freshness unknown" at random."""
        truncated = ZKX[:ZKX.index("List:")]
        self.assertIsNone(irc.parse_channel_advert(truncated).get("list_date"),
                          "the fixture still carries a date - nothing is tested")

        self.capture("Zkx", ZKX, now=T0)
        self.capture("Zkx", truncated, now=T0 + 300)

        self.assertEqual(self.entry("Zkx")["list_date"], "Aug 10th")


class TheWholeCapturedSample(CaptureTestCase):
    """All 33 through the real capture path, not just the parser."""

    def setUp(self):
        super().setUp()
        for nick, text in ALL_CAPTURED:
            self.capture(nick, text)

    def test_every_bot_is_registered_once(self):
        self.assertEqual(len(runtime.known_bots), len(ALL_CAPTURED))
        for nick, _text in ALL_CAPTURED:
            self.assertIn(nick.lower(), runtime.known_bots)

    def test_each_entry_carries_what_that_bot_published(self):
        self.assertEqual(self.entry("[rigserv]")["files"], 77018)
        self.assertEqual(self.entry("Q_R_Q")["list_date"], "Aug 28th")
        self.assertEqual(self.entry("DCCoreWin")["list_size"], "1.21TB")
        self.assertEqual(self.entry("BigRig")["list_size"], "163812MB")

    def test_every_entry_knows_where_and_when_it_was_seen(self):
        for nick, entry in runtime.known_bots.items():
            with self.subTest(nick=nick):
                self.assertEqual(entry["channel"], "#DCCore-Test")
                self.assertEqual(entry["last_seen"], T0)

    def test_the_stored_nick_keeps_the_bot_s_own_casing(self):
        """Keyed lowercase because IRC nicks are case-insensitive, displayed as
        the bot writes it - "@DCCoreWin", not "@dccorewin"."""
        self.assertEqual(self.entry("dccorewin")["nick"], "DCCoreWin")
        self.assertEqual(self.entry("disco_pal")["nick"], "disco_pal")

    def test_most_of_the_channel_publishes_a_date(self):
        """A sanity floor on the sample as a whole. If a change made the date
        pattern miss a whole family, the per-bot tests above would each still
        pass for the families they name while the channel as a whole went
        dark."""
        dated = [e for e in runtime.known_bots.values() if e.get("list_date")]

        self.assertGreaterEqual(len(dated), 30, "the date pattern lost a family")


class SurvivingARestart(CaptureTestCase):
    """An empty sidebar until every bot has advertised again is a long wait on
    a five-minute cycle, so the registry is written to disk."""

    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.make_tree().root, "known_bots.json")
        self.set_config(KNOWN_BOTS_FILE=self.path)
        previous = db.KNOWN_BOTS_FILE
        db.KNOWN_BOTS_FILE = self.path
        self.addCleanup(setattr, db, "KNOWN_BOTS_FILE", previous)
        runtime.known_bots_flushed_at = 0.0

    def test_a_registry_round_trips(self):
        self.capture("Zkx", ZKX)
        irc._flush_known_bots(force=True)
        runtime.known_bots.clear()

        runtime.known_bots.update(db.load_known_bots())

        self.assertEqual(runtime.known_bots["zkx"]["files"], 719041)
        self.assertEqual(runtime.known_bots["zkx"]["list_date"], "Aug 10th")

    def test_both_of_a_bot_s_lists_survive(self):
        self.capture("Zkx", ZKX, now=T0)
        self.capture("Zkx", ZKX_RAR, now=T0 + 60)
        irc._flush_known_bots(force=True)
        runtime.known_bots.clear()

        runtime.known_bots.update(db.load_known_bots())

        self.assertEqual(runtime.known_bots["zkx"]["files"], 719041)
        self.assertEqual(runtime.known_bots["zkx"]["rar_folders"], 39454)

    def test_no_file_yet_is_an_empty_registry_not_an_error(self):
        self.assertEqual(db.load_known_bots(), {})

    def test_an_unreadable_registry_does_not_stop_the_daemon(self):
        """It is rebuilt from adverts within minutes, so a corrupt file costs
        an empty sidebar until then and nothing else."""
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json at all")

        self.assertEqual(db.load_known_bots(), {})

    def test_writes_are_throttled(self):
        """Thirty-three bots on a five-minute cycle is a write every ten
        seconds for nothing - the file is read once, at startup."""
        self.capture("Zkx", ZKX)                      # first flush
        written_at = os.path.getmtime(self.path)
        stamp = runtime.known_bots_flushed_at

        wrote = irc._flush_known_bots(now=stamp + 1.0)

        self.assertFalse(wrote, "a second write landed inside the throttle window")
        self.assertEqual(os.path.getmtime(self.path), written_at)

    def test_the_throttle_lets_a_later_write_through(self):
        self.capture("Zkx", ZKX)
        stamp = runtime.known_bots_flushed_at

        self.assertTrue(
            irc._flush_known_bots(now=stamp + irc.KNOWN_BOTS_FLUSH_SECONDS + 1))


class TheListenerIsActuallyWired(unittest.TestCase):
    """A parser nothing calls collects nothing.

    This is the shape of #119 - a correct, tested function that no live path
    reached, so the speed record stayed at zero for the life of the daemon.
    Unit tests cannot see that, so it gets asked directly.
    """

    def _source(self, name):
        with open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
            return [line for line in handle.read().splitlines()
                    if not line.strip().startswith("#")]

    def test_channel_messages_reach_the_capture(self):
        calls = [line.strip() for line in self._source("irc.py")
                 if "_capture_channel_advert(" in line and "def " not in line]

        self.assertTrue(
            calls,
            "nothing calls _capture_channel_advert. It belongs on the channel "
            "PRIVMSG path beside _capture_broadcast_search_reply; without that "
            "call the registry is never populated and the parser is dead code.")

    def test_the_capture_sits_beside_the_broadcast_capture(self):
        """Both read passively from the same channel traffic for the same
        reason. Splitting them means one of the two gets a ban check or a
        dispatch guard bolted on and the other silently does not."""
        lines = self._source("irc.py")
        advert = [i for i, line in enumerate(lines)
                  if "_capture_channel_advert(" in line and "def " not in line]
        search = [i for i, line in enumerate(lines)
                  if "_capture_broadcast_search_reply(" in line and "def " not in line]
        self.assertTrue(advert and search, "could not locate both capture calls")

        self.assertLessEqual(
            min(abs(a - s) for a in advert for s in search), 4,
            "the advert capture has drifted away from the broadcast capture")

    def test_the_registry_is_loaded_at_startup(self):
        loads = [line.strip() for line in self._source("oserve.py")
                 if "load_known_bots(" in line]

        self.assertTrue(
            loads,
            "oserve.py no longer loads the bot registry, so persisting it "
            "buys nothing - the sidebar is empty on every restart until all "
            "33 have advertised again.")


if __name__ == "__main__":
    unittest.main()
