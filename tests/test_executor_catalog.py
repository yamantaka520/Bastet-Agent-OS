from bastet_agent_os import maintenance
from bastet_agent_os.executors import accounts
from bastet_agent_os.executors.base import route_incompatibility
from bastet_agent_os.executors.openclaw import OpenClawExecutor
from bastet_agent_os.executors.pi_agent import PiExecutor


def test_pi_and_openclaw_are_first_class_executor_accounts():
    catalog = {row["kind"]: row for row in accounts.EXECUTOR_CATALOG}
    assert catalog["pi"]["binary"] == "pi"
    assert catalog["openclaw"]["binary"] == "openclaw"
    assert accounts.HOME_ENV["pi"] == "PI_CODING_AGENT_DIR"
    assert accounts.HOME_ENV["openclaw"] == "OPENCLAW_HOME"
    assert accounts.login_command("pi", "/profiles/pi")[0] == {
        "PI_CODING_AGENT_DIR": "/profiles/pi"}
    assert accounts.login_command("openclaw", "/profiles/claw")[0] == {
        "OPENCLAW_HOME": "/profiles/claw"}


def test_pi_and_openclaw_are_maintainable_components():
    components = {row["id"]: row for row in maintenance.CLI_COMPONENTS}
    assert "pi.dev/install.sh" in components["pi"]["update"]
    assert "--no-onboard" in components["openclaw"]["update"]


def test_route_contract_advertises_only_proven_capabilities():
    pi = PiExecutor()
    assert route_incompatibility(
        pi, has_gateway=True, api_flavor="anthropic", model="claude-test",
        read_only=True) is None

    claw = OpenClawExecutor()
    assert "read-only" in route_incompatibility(
        claw, has_gateway=False, api_flavor=None, model=None, read_only=True)
    assert "does not support Bastet Gateway" in route_incompatibility(
        claw, has_gateway=True, api_flavor="openai", model="gpt-test",
        read_only=False)
