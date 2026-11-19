from router.services.opencode import OpencodeVersionService


def test_opencode_blocks_old_versions():
    blocked, version = OpencodeVersionService.should_block("foo opencode/1.19.6 bar")
    assert blocked is True
    assert version == "1.19.6"


def test_opencode_allows_newer_versions():
    blocked, version = OpencodeVersionService.should_block("opencode/1.19.7")
    assert blocked is False
    assert version == "1.19.7"


def test_opencode_delays_any_failure_for_opencode_clients():
    assert OpencodeVersionService.should_delay_failure("opencode/1.2.27", 400) is True
    assert OpencodeVersionService.should_delay_failure("opencode/1.2.30", 502) is True
    assert OpencodeVersionService.should_delay_failure("opencode/1.2.30", 504) is True


def test_opencode_does_not_delay_success_or_non_opencode():
    assert OpencodeVersionService.should_delay_failure("opencode/1.2.30", 200) is False
    assert OpencodeVersionService.should_delay_failure("curl/8.0.0", 500) is False
    assert OpencodeVersionService.should_delay_failure(None, 500) is False
