import pytest


@pytest.fixture(autouse=True)
def ini(pytester: pytest.Pytester) -> None:
    # The inner runs load pytest-asyncio too, which warns without this.
    pytester.makeini("[pytest]\nasyncio_default_fixture_loop_scope = function\n")


def test_every_kind_of_skip_fails_the_run(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_module_skip="import pytest\npytest.skip('later', allow_module_level=True)\n",
        test_importorskip="import pytest\npytest.importorskip('no_such_module_here')\n",
        test_mark="import pytest\n\n@pytest.mark.skip(reason='x')\ndef test_mark():\n    pass\n",
        test_xfail="import pytest\n\n@pytest.mark.xfail\ndef test_xfail():\n    assert False\n",
        test_ok="def test_ok():\n    pass\n",
    )
    result = pytester.runpytest("-p", "noskip")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines_random(
        [
            "*skipped, which fails the run: test_module_skip.py",
            "*skipped, which fails the run: test_importorskip.py",
            "*skipped, which fails the run: test_mark.py::test_mark",
            "*skipped, which fails the run: test_xfail.py::test_xfail",
        ]
    )


def test_a_run_of_skipped_modules_alone_fails(pytester: pytest.Pytester) -> None:
    pytester.makepyfile("import pytest\npytest.skip('later', allow_module_level=True)\n")
    assert pytester.runpytest("-p", "noskip").ret == pytest.ExitCode.TESTS_FAILED


def test_a_run_without_skips_passes(pytester: pytest.Pytester) -> None:
    pytester.makepyfile("def test_ok():\n    pass\n")
    assert pytester.runpytest("-p", "noskip").ret == pytest.ExitCode.OK
