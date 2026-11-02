import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_json(relative_path):
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def test_deploy_button_uses_complete_repository():
    button = (
        "[![Deploy to Cloudflare](https://deploy.workers.cloudflare.com/button)]"
        "(https://deploy.workers.cloudflare.com/?url="
        "https://github.com/utlastr/GameRadar-cloudflare)"
    )
    for relative_path in ("README.md", "cloudflare/README.md"):
        assert button in (ROOT / relative_path).read_text(encoding="utf-8")


def test_root_configuration_preserves_python_build_context():
    configuration = read_json("wrangler.jsonc")
    assert (ROOT / configuration["main"]).is_file()
    container = configuration["containers"][0]
    context = (ROOT / container["image_build_context"]).resolve()
    assert context == ROOT
    assert (ROOT / container["image"]).is_file()
    assert (context / "radar" / "server.py").is_file()
    assert (context / "run_radar.py").is_file()


def test_workspace_commands_use_single_root_configuration():
    root_package = read_json("package.json")
    worker_package = read_json("cloudflare/package.json")
    assert root_package["workspaces"] == ["cloudflare"]
    assert root_package["scripts"]["deploy"] == (
        "npm run deploy --workspace=gameradar-cloudflare"
    )
    for command in ("dev", "types", "deploy"):
        assert "--config ../wrangler.jsonc" in worker_package["scripts"][command]


def test_secret_examples_match_deployment_prompts():
    examples = {}
    for line in (ROOT / ".dev.vars.example").read_text(encoding="utf-8").splitlines():
        key, value = line.split("=", 1)
        examples[key] = value
    bindings = read_json("package.json")["cloudflare"]["bindings"]
    assert set(examples) == set(bindings)
    assert len(examples) == 6
    assert examples["CF_ACCESS_TEAM_DOMAIN"] == "your-team.cloudflareaccess.com"
    for key, value in examples.items():
        if key != "CF_ACCESS_TEAM_DOMAIN":
            assert value.startswith("replace-with-")


def test_root_lockfile_tracks_workspace_dependencies():
    lockfile = read_json("package-lock.json")
    assert lockfile["packages"][""]["workspaces"] == ["cloudflare"]
    worker_package = read_json("cloudflare/package.json")
    for kind in ("dependencies", "devDependencies"):
        assert lockfile["packages"]["cloudflare"][kind] == worker_package[kind]
