import pytest

from pmsim.cli.main import main


def test_main_no_args_ok() -> None:
    assert main([]) == 0


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "pmsim" in capsys.readouterr().out
