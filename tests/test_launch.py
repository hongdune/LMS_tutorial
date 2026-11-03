from lms_kernel.launch import build_command


def test_build_command_restricts_origin_and_binds_loopback():
    cmd = build_command("http://lms.example:3000", 8899, "secret-token")
    assert "--ServerApp.ip=127.0.0.1" in cmd
    assert "--ServerApp.allow_origin=http://lms.example:3000" in cmd
    assert "--IdentityProvider.token=secret-token" in cmd
    assert "--port=8899" in cmd
    assert not any("disable_check_xsrf" in arg for arg in cmd)
    assert (
        "--ServerApp.identity_provider_class=lms_kernel.auth.StrictOriginIdentityProvider" in cmd
    )


def test_debug_drop_reply_once_only_when_requested():
    key = "--ServerApp.kernel_websocket_connection_class=lms_kernel.debug.DropReplyOnceConnection"
    assert key not in build_command("http://lms.example:3000", 8899, "t")
    assert key in build_command("http://lms.example:3000", 8899, "t", debug_drop_reply_once=True)
