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


CULL_ARGS = (
    "--MappingKernelManager.cull_interval=300",
    "--MappingKernelManager.cull_connected=True",
    "--MappingKernelManager.cull_busy=False",
)


def test_local_idle_timeout_is_off_by_default():
    # Contract 2.1 (v0.8): local default 0.
    cmd = build_command("http://lms.example:3000", 8899, "t")
    assert not any("MappingKernelManager" in arg for arg in cmd)
    cmd = build_command("http://lms.example:3000", 8899, "t", idle_timeout_minutes=0)
    assert not any("MappingKernelManager" in arg for arg in cmd)


def test_local_idle_timeout_minutes():
    cmd = build_command("http://lms.example:3000", 8899, "t", idle_timeout_minutes=10)
    assert "--MappingKernelManager.cull_idle_timeout=600" in cmd
    for arg in CULL_ARGS:
        assert arg in cmd


def test_allow_root_follows_effective_user(monkeypatch):
    key = "--ServerApp.allow_root=True"
    assert key in build_command("http://lms.example:3000", 8899, "t", allow_root=True)
    assert key not in build_command("http://lms.example:3000", 8899, "t", allow_root=False)
    monkeypatch.setattr("lms_kernel.launch.os.geteuid", lambda: 0, raising=False)
    assert key in build_command("http://lms.example:3000", 8899, "t")
    monkeypatch.setattr("lms_kernel.launch.os.geteuid", lambda: 1000, raising=False)
    assert key not in build_command("http://lms.example:3000", 8899, "t")


def test_ctrl_c_is_left_to_lms_runtime_and_port_is_fixed():
    cmd = build_command("http://lms.example:3000", 8899, "t")
    assert "--LMSServerApp.ignore_sigint=True" in cmd
    assert "--ServerApp.port_retries=0" in cmd
