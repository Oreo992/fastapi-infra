import os
from pathlib import Path

from infra.plugins.template import SUPPORTED_PROVIDER_KINDS
from scripts import smoke_plugin_templates as module


def test_plugin_template_smoke_script_covers_every_provider_kind():
    assert set(module.PROVIDER_EXAMPLES) == set(SUPPORTED_PROVIDER_KINDS)


def test_plugin_template_smoke_script_installs_to_private_target(tmp_path, monkeypatch):
    package_dir = tmp_path / "plugin"
    target_dir = tmp_path / "target"
    package_dir.mkdir()
    (package_dir / "pyproject.toml").write_text("[project]\nname = 'plugin'\n")
    commands = []

    def fake_run(command, *, cwd, timeout, env=None):
        commands.append((command, cwd, timeout, env))

    monkeypatch.setattr(module, "_run", fake_run)

    env = module._install_editable(
        package_dir,
        target_dir=target_dir,
        python=Path("python"),
        timeout=7,
    )

    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(target_dir.resolve())
    assert commands == [
        (
            [
                "python",
                "-m",
                "pip",
                "install",
                "-e",
                str(package_dir),
                "--no-deps",
                "--no-build-isolation",
                "--target",
                str(target_dir),
            ],
            package_dir,
            7,
            None,
        )
    ]


def test_plugin_template_smoke_script_private_target_includes_editable_pth_paths(
    tmp_path, monkeypatch
):
    target_dir = tmp_path / "target"
    src_dir = tmp_path / "plugin" / "src"
    target_dir.mkdir()
    src_dir.mkdir(parents=True)
    (target_dir / "editable.pth").write_text(
        f"# ignored\n{src_dir}\nimport editable_hook\nrelative-src\n",
        encoding="utf-8",
    )
    (target_dir / "relative-src").mkdir()
    monkeypatch.setenv("PYTHONPATH", "existing-path")

    entries = module._pythonpath_entries_for_target(target_dir)
    env = module._pythonpath_env(entries)

    assert entries == [
        target_dir.resolve(),
        src_dir.resolve(),
        (target_dir / "relative-src").resolve(),
    ]
    assert env["PYTHONPATH"] == os.pathsep.join(
        [
            str(target_dir.resolve()),
            str(src_dir.resolve()),
            str((target_dir / "relative-src").resolve()),
            "existing-path",
        ]
    )
