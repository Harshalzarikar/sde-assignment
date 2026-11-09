def pytest_configure(config):
    config.addinivalue_line("markers", "slow: drives the real simulator end to end (about 10 seconds)")
