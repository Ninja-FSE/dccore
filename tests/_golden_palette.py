"""What every themed path puts on the wire, pinned.

Captured through tests/test_theme.py's own harness with the clock frozen AND
rendered in UTC. These are the bytes the classic theme must produce: a
refactor of the look that alters one of them is not a refactor.

MOVED ONCE, DELIBERATELY. The original capture was of the code as it stood
before theme.py existed, and held until bold was removed from every outbound
path - asked for directly: "theme shouldn't have bold in any location of the
message ... No bolds."

That move was made by stripping \\x02 from the strings below and by nothing
else, after checking that stripping it from the OLD line reproduced the NEW
line exactly, for all eight paths. Re-capturing would have been the easy way
and the wrong one: a fresh capture also absorbs anything else that has drifted
since, which is the failure this file exists to prevent.

Do not hand-edit. To move it again, prove the whole difference first the same
way, then apply exactly that difference.
"""

GOLDEN = {
    'list.execute_search': [
        'PRIVMSG dave :\x0304,05 \x0310,10 \x0301,00 Search Result: \x0303ON \x0310,10 \x0304,05 \x0301,00 Found: \x03041 Match(es) For \x0303metallica \x0310,10 \x0304,05 \x0301,00 Sending: \x03041 \x0310,10 \x0304,05 \x0301,00 Slots: \x03035/5 Free \x0310,10 \x0304,05 \x0301,00 Queued: \x03030 \x0310,10 \x0304,05 \r\n',
        'PRIVMSG dave :\x0310,10 \x0304,05 \x0301,00 !DCCoreTest Metallica - Enter Sandman.flac  ::INFO:: 4.0MB\x0f \x0310,10 \x0304,05 \r\n',
    ],
    'list.send_list_trigger_info': [
        'NOTICE dave :List trigger(s): \x0304@DCCoreTest\x03 vTest\x03\r\n',
    ],
    'send_dcc_queue_notice': [
        'NOTICE dave :\x0310,10 \x0304,05 \x0301,00 Added Enter Sandman.flac to your personal queue at position #2 of 100.\x0f \x0310,10 \x0304,05 \r\n',
    ],
    'send_dcc_sending_notice': [
        'NOTICE dave :\x0304,05 \x0310,10 \x0301,00 Sending: Enter Sandman.flac \x0310,10 \x0304,05 \x0301,00 Status: \x0303Active Transfer Started \x0310,10 \x0304,05 \r\n',
    ],
    'send_debug': [
        'PRIVMSG #dccore-debug :\x0304,05 \x0310,10 \x0301,00 [22:13:20] DEBUG \x0310,10 \x0304,05 \x0301,00 Category: \x0314[INFO]\x0f\x0301,00 \x0310,10 \x0304,05 \x0301,00 Log: a debug line \x0310,10 \x0304,05 \x0f\r\n',
    ],
    'send_pack_error_notice': [
        'NOTICE dave :\x0304,05 \x0310,10 \x0301,00 DCC-PACK: Access Denied \x0310,10 \x0304,05 \x0301,00 Error: Artist root folders cannot be requested. Please select a specific album sub-folder. \x0310,10 \x0304,05 \r\n',
    ],
    'send_search_result_header': [
        'PRIVMSG dave :\x0304,05 \x0310,10 \x0301,00 Search Result: \x0303ON \x0310,10 \x0304,05 \x0301,00 Found: \x03043 Match(es) For \x0303metallica \x0310,10 \x0304,05 \x0301,00 Sending: \x03043 \x0310,10 \x0304,05 \x0301,00 Slots: \x03035/5 Free \x0310,10 \x0304,05 \x0301,00 Queued: \x03030 \x0310,10 \x0304,05 \r\n',
    ],
    'send_transfer_complete': [
        'PRIVMSG #dccore-test :\x0304,05 \x0310,10 \x0301,00 \x0303Sent\x0301,00: Enter Sandman.flac \x0310,10 \x0304,05 \x0301,00 To: \x0303dave \x0310,10 \x0304,05 \x0301,00 Total Sent: \x0303100 Files (200.0B) \x0310,10 \x0304,05 \x0301,00 Yesterday: \x03040 Files \x0310,10 \x0304,05 \x0301,00 Today: \x03040 Files \x0312[as of 10:13 pm] \x0310,10 \x0304,05 \x0301,00 Speed: \x0303500.0k/s \x0310,10 \x0304,05 \r\n',
    ],
}
