"""What every themed path put on the wire BEFORE theme.py existed.

Captured through tests/test_theme.py's own harness against the unmodified
announce.py and list.py, with the clock frozen AND rendered in UTC. These
are the bytes the classic theme must still produce: a refactor of the look
that alters one of them is not a refactor.

Do not hand-edit. Regenerate by running the harness against a checkout of
the code as it was before the change being verified.
"""

GOLDEN = {
    'list.execute_search': [
        'PRIVMSG dave :\x0304,05 \x0310,10 \x0301,00 Search Result: \x02\x0303ON\x02 \x0310,10 \x0304,05 \x0301,00 Found: \x02\x03041\x02 Match(es) For \x02\x0303metallica\x02 \x0310,10 \x0304,05 \x0301,00 Sending: \x02\x03041\x02 \x0310,10 \x0304,05 \x0301,00 Slots: \x02\x03035/5\x02 Free \x0310,10 \x0304,05 \x0301,00 Queued: \x02\x03030\x02 \x0310,10 \x0304,05 \r\n',
        'PRIVMSG dave :\x0310,10 \x0304,05 \x0301,00 !DCCoreTest Metallica - Enter Sandman.flac  ::INFO:: 4.0MB\x0f \x0310,10 \x0304,05 \r\n',
    ],
    'list.send_list_trigger_info': [
        'NOTICE dave :List trigger(s): \x0304@DCCoreTest\x03 vTest\x03\r\n',
    ],
    'send_dcc_queue_notice': [
        'NOTICE dave :\x0310,10 \x0304,05 \x0301,00 Added Enter Sandman.flac to your personal queue at position #2 of 100.\x0f \x0310,10 \x0304,05 \r\n',
    ],
    'send_dcc_sending_notice': [
        'NOTICE dave :\x0304,05 \x0310,10 \x0301,00 Sending: \x02Enter Sandman.flac\x02 \x0310,10 \x0304,05 \x0301,00 Status: \x02\x0303Active Transfer Started\x02 \x0310,10 \x0304,05 \r\n',
    ],
    'send_debug': [
        'PRIVMSG #dccore-debug :\x0304,05 \x0310,10 \x0301,00 [22:13:20] \x02DEBUG\x02 \x0310,10 \x0304,05 \x0301,00 \x02Category\x02: \x0314[INFO]\x0f\x0301,00 \x0310,10 \x0304,05 \x0301,00 Log: a debug line \x0310,10 \x0304,05 \x0f\r\n',
    ],
    'send_pack_error_notice': [
        'NOTICE dave :\x0304,05 \x0310,10 \x0301,00 DCC-PACK: \x02Access Denied\x02 \x0310,10 \x0304,05 \x0301,00 Error: \x02Artist root folders cannot be requested. Please select a specific album sub-folder.\x02 \x0310,10 \x0304,05 \r\n',
    ],
    'send_search_result_header': [
        'PRIVMSG dave :\x0304,05 \x0310,10 \x0301,00 Search Result: \x02\x0303ON\x02 \x0310,10 \x0304,05 \x0301,00 Found: \x02\x03043\x02 Match(es) For \x02\x0303metallica\x02 \x0310,10 \x0304,05 \x0301,00 Sending: \x02\x03043\x02 \x0310,10 \x0304,05 \x0301,00 Slots: \x02\x03035/5\x02 Free \x0310,10 \x0304,05 \x0301,00 Queued: \x02\x03030\x02 \x0310,10 \x0304,05 \r\n',
    ],
    'send_transfer_complete': [
        'PRIVMSG #dccore-test :\x0304,05 \x0310,10 \x0301,00 \x02\x0303Sent\x02\x0301,00: \x02Enter Sandman.flac\x02 \x0310,10 \x0304,05 \x0301,00 To: \x02\x0303dave\x02 \x0310,10 \x0304,05 \x0301,00 Total Sent: \x02\x0303100 Files (200.0B)\x02 \x0310,10 \x0304,05 \x0301,00 Yesterday: \x02\x03040 Files\x02 \x0310,10 \x0304,05 \x0301,00 Today: \x02\x03040 Files\x02 \x0312[as of 10:13 pm] \x0310,10 \x0304,05 \x0301,00 Speed: \x02\x0303500.0k/s\x02 \x0310,10 \x0304,05 \r\n',
    ],
}
