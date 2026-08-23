from bastet_agent_os.config import Home


def test_server_url_turns_wildcard_bind_into_loopback(tmp_path):
    home = Home(tmp_path / "bastet")
    home.ensure()
    home.save_config({"host": "0.0.0.0", "port": 8890})
    assert home.server_url() == "http://127.0.0.1:8890"

    home.save_config({"host": "::", "port": 8890})
    assert home.server_url() == "http://[::1]:8890"


def test_server_url_keeps_a_concrete_host(tmp_path):
    home = Home(tmp_path / "bastet")
    home.ensure()
    home.save_config({"host": "192.168.100.250", "port": 9999})
    assert home.server_url() == "http://192.168.100.250:9999"
