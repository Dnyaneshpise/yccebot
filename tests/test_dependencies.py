

# ----------------------------------------------------------------------
# Dependency declarations must match what the suite actually needs
# ----------------------------------------------------------------------
def test_async_plugin_is_declared():
    """pytest-asyncio must be in requirements.txt.

    The suite has async tests and pytest.ini sets asyncio_mode=auto. The
    plugin is easy to have installed locally and forget to declare, which
    only fails in a clean environment (CI).
    """
    from pathlib import Path

    requirements = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text(
        encoding="utf-8"
    )
    assert "pytest-asyncio" in requirements, (
        "pytest-asyncio is required by the async tests but is not declared in "
        "requirements.txt; CI installs only what is declared"
    )


def test_asyncio_mode_is_configured():
    """pytest.ini must enable asyncio_mode=auto for the async tests."""
    from pathlib import Path

    pytest_ini = Path(__file__).resolve().parent.parent / "pytest.ini"
    assert pytest_ini.exists(), "pytest.ini is missing"
    assert "asyncio_mode" in pytest_ini.read_text(encoding="utf-8")


def test_every_module_import_is_satisfied_locally():
    """Guard against importing a package that is not installed at all."""
    import importlib

    for module in ("agent.main", "agent.builder", "agent.ai", "agent.telegram",
                   "agent.discovery", "agent.actions", "agent.executors",
                   "agent.storage", "agent.rewards", "agent.config"):
        assert importlib.import_module(module) is not None

    # playwright is imported lazily inside functions, so assert it is
    # installed rather than already in sys.modules.
    assert importlib.util.find_spec("playwright") is not None