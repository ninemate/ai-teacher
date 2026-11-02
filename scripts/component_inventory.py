#!/usr/bin/env python3
"""Write an offline inventory of installed and configured application components."""

import argparse
import importlib.metadata
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import backup_restore

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ("api", "ingestion", "voice")
MODEL_METADATA = r"""import importlib.metadata as m, json, sys
names=json.loads(sys.argv[1]); out={}
for name in names:
 try:
  d=m.distribution(name); md=d.metadata; urls=md.get_all('Project-URL') or []
  license=md.get('License') or next((x.rsplit('::',1)[-1] for x in md.get_all('Classifier') or [] if x.startswith('License ::')), 'Not declared in package metadata')
  source=md.get('Home-page') or next((x.split(', ',1)[-1] for x in urls if x.lower().startswith(('source,','homepage,','repository,'))), 'Not declared in package metadata')
  out[name]={'version':d.version,'license':license,'source':source,'dependencies':d.requires or []}
 except m.PackageNotFoundError: out[name]={'version':'not installed in this image','license':'not checked','source':'not available','dependencies':[]}
print(json.dumps(out))"""
OLLAMA_TAGS = r"""import json,os,urllib.request
u=os.environ.get('OLLAMA_BASE_URL','http://ollama:11434').rstrip('/')+'/api/tags'
print(json.dumps(json.load(urllib.request.urlopen(u,timeout=15)).get('models',[])))"""
MODEL_CACHE = r"""import json,os
from pathlib import Path
home=Path(os.environ.get('HF_HOME',Path.home()/'.cache'/'huggingface'))
ids=['intfloat/multilingual-e5-base','Systran/faster-whisper-large-v3-turbo']
out={}
for name in ids:
 p=home/'hub'/('models--'+name.replace('/','--'))/'refs'/'main'
 out[name]=p.read_text().strip() if p.is_file() else 'not present in local cache'
print(json.dumps(out))"""
PIPER_FILES = r"""import hashlib,json,os,sys
from pathlib import Path
root=Path(os.environ.get('VOICE_TTS_VOICE_DIR','/models/piper'))
names=json.loads(sys.argv[1]); out={}
for name in names:
 row={}
 for suffix in ('.onnx','.onnx.json'):
  path=root/(name+suffix)
  if path.is_file():
   digest=hashlib.sha256(path.read_bytes()).hexdigest()
   row[suffix.lstrip('.')]=digest
   if suffix.endswith('.json'):
    try:
     metadata=json.loads(path.read_text(encoding='utf-8'))
     if metadata.get('license'): row['license']=metadata['license']
    except (OSError,ValueError): pass
 out[name]=row
print(json.dumps(out))"""


def requirement_lines(service: str) -> list[tuple[str, str]]:
    path = ROOT / "services" / service / "requirements.txt"
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        match = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?\s*(.*)$", line)
        if match:
            rows.append((match.group(1), match.group(2) or "unversioned"))
    return rows


def dependency_names(requires: list[str] | None) -> str:
    names = []
    for requirement in requires or []:
        if "extra ==" in requirement:
            continue
        match = re.match(r"\s*([A-Za-z0-9_.-]+)", requirement)
        if match and match.group(1).lower() not in {item.lower() for item in names}:
            names.append(match.group(1))
    return ", ".join(names) or "none declared"


def compose_prefix(settings: backup_restore.Settings) -> list[str]:
    command = ["docker", "compose"]
    if settings.env_file.is_file():
        command += ["--env-file", str(settings.env_file)]
    for name in ("docker-compose.yml", "docker-compose.dev.yml", "docker-compose.proxy.yml"):
        file = settings.stack_dir / "compose" / name
        if file.is_file():
            command += ["-f", str(file)]
    voice_enabled = os.environ.get("TEACHER_ENABLE_VOICE", settings.env.get("TEACHER_ENABLE_VOICE", "false"))
    voice_file = settings.stack_dir / "compose" / "docker-compose.voice.example.yml"
    if voice_enabled.lower() == "true" and voice_file.is_file():
        command += ["-f", str(voice_file)]
    return command


def compose_python(settings: backup_restore.Settings, service: str, code: str, argument: str = ""):
    command = compose_prefix(settings) + ["exec", "-T", service, "python", "-c", code]
    if argument:
        command.append(argument)
    try:
        return subprocess.run(command, cwd=settings.stack_dir, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None


def installed_packages(settings: backup_restore.Settings) -> list[dict]:
    rows: dict[tuple[str, str], dict] = {}
    for service in REQUIREMENTS:
        declared = requirement_lines(service)
        names = sorted({name for name, _ in declared})
        result = compose_python(settings, service, MODEL_METADATA, json.dumps(names)) if settings.transport == "compose" else None
        image_query_succeeded = bool(result and result.returncode == 0)
        if image_query_succeeded:
            metadata = json.loads(result.stdout)
            location = f"{service} container"
        else:
            metadata, location = {}, "inventory host"
            if settings.transport == "compose":
                location = f"host fallback; {service} image metadata unavailable"
        for name, constraint in declared:
            details = metadata.get(name) or metadata.get(name.lower())
            if details is None:
                try:
                    distribution = importlib.metadata.distribution(name)
                    package_meta = distribution.metadata
                    classifiers = package_meta.get_all("Classifier") or []
                    license_name = package_meta.get("License") or next(
                        (item.rsplit("::", 1)[-1] for item in classifiers if item.startswith("License ::")),
                        "Not declared in package metadata",
                    )
                    urls = package_meta.get_all("Project-URL") or []
                    source = package_meta.get("Home-page") or next(
                        (item.split(", ", 1)[-1] for item in urls if item.lower().startswith(("source,", "homepage,", "repository,"))),
                        "Not declared in package metadata",
                    )
                    details = {
                        "version": distribution.version,
                        "license": license_name,
                        "source": source,
                        "dependencies": distribution.requires or [],
                    }
                except importlib.metadata.PackageNotFoundError:
                    if settings.transport == "compose" and not image_query_succeeded:
                        version = "not found on inventory host; image status unavailable"
                    elif settings.transport == "compose":
                        version = "not reported by image metadata or inventory host"
                    else:
                        version = "not found on inventory host; service image not queried"
                    details = {
                        "version": version,
                        "license": "not checked",
                        "source": "not available",
                        "dependencies": [],
                    }
            key = (name.lower(), details["version"])
            if key not in rows:
                rows[key] = {
                    "name": name,
                    "version": details["version"],
                    "license": details["license"],
                    "source": details["source"],
                    "checksum": "not available from installed package metadata",
                    "dependencies": dependency_names(details["dependencies"]),
                    "install_date": "not recorded by pip metadata",
                    "rollback_path": "rebuild the recorded service image from its requirements file",
                    "declared": constraint,
                    "location": location,
                }
            elif rows[key]["declared"] != constraint:
                rows[key]["declared"] += f", {constraint}"
                rows[key]["location"] += f", {location}"
    return sorted(rows.values(), key=lambda row: row["name"].lower())


def ollama_models(settings: backup_restore.Settings) -> tuple[list[dict], str]:
    models = None
    if settings.transport == "compose":
        result = compose_python(settings, "api", OLLAMA_TAGS)
        if result and result.returncode == 0:
            models = json.loads(result.stdout)
    if models is None:
        env = settings.env
        url = os.environ.get("OLLAMA_BASE_URL", env.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip("/")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            return [], "Ollama API not queried: fallback is limited to host loopback; no remote endpoint was contacted"
        try:
            with urllib.request.urlopen(url + "/api/tags", timeout=5) as response:
                models = json.load(response).get("models", [])
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return [], "Ollama API unavailable; no network or model installation was attempted"
    return models, "installed models returned by Ollama /api/tags"


def model_cache(settings: backup_restore.Settings) -> dict[str, str]:
    result = compose_python(settings, "api", MODEL_CACHE) if settings.transport == "compose" else None
    voice_result = compose_python(settings, "voice", MODEL_CACHE) if settings.transport == "compose" else None
    if result and result.returncode == 0:
        cache = json.loads(result.stdout)
        if voice_result and voice_result.returncode == 0:
            cache.update(json.loads(voice_result.stdout))
        return cache
    return {
        "intfloat/multilingual-e5-base": "not queried in host cache",
        "Systran/faster-whisper-large-v3-turbo": "not queried in host cache",
    }


def piper_model_files(settings: backup_restore.Settings, voices: list[str]) -> dict[str, dict[str, str]]:
    result = compose_python(settings, "voice", PIPER_FILES, json.dumps(voices)) if settings.transport == "compose" else None
    if result and result.returncode == 0:
        return json.loads(result.stdout)
    return {}


def image_rows(settings: backup_restore.Settings) -> list[dict]:
    service_rows = []
    for service in ("api", "ingestion", "qdrant", "ollama", "proxy", "voice", "egress_probe"):
        try:
            result = subprocess.run(
                compose_prefix(settings) + ["images", "-q", service],
                cwd=settings.stack_dir,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            ) if settings.transport == "compose" else None
        except (OSError, subprocess.SubprocessError):
            result = None
        image_id = result.stdout.splitlines()[0].strip() if result and result.returncode == 0 and result.stdout.strip() else ""
        declared_ref = image_id
        if not image_id:
            compose_text = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "compose").glob("docker-compose*.yml"))
            image_match = re.search(rf"(?ms)^  {re.escape(service)}:\n(?:(?!^  [A-Za-z0-9_-]+:).)*?^\s+image:\s*([^\s#]+)", compose_text)
            declared_ref = image_match.group(1) if image_match else "built image not found locally"
            inspect_ref = declared_ref if declared_ref != "built image not found locally" else ""
        else:
            inspect_ref = image_id
        image_digest = "unavailable"
        version = declared_ref
        source = declared_ref
        if inspect_ref:
            try:
                inspection = subprocess.run(["docker", "image", "inspect", inspect_ref], capture_output=True, text=True, timeout=20, check=False)
            except (OSError, subprocess.SubprocessError):
                inspection = None
            if inspection and inspection.returncode == 0:
                details = json.loads(inspection.stdout)[0]
                image_digest = (details.get("RepoDigests") or [details.get("Id", "unavailable")])[0]
                if declared_ref.startswith("sha256:"):
                    tags = details.get("RepoTags") or []
                    version = tags[0] if tags else declared_ref
                    source = (details.get("RepoDigests") or tags or [declared_ref])[0]
        service_rows.append(
            {
                "name": f"container image: {service}",
                "version": version,
                "license": "see image publisher's license; not checked by this report",
                "source": source,
                "checksum": image_digest,
                "dependencies": "Docker Engine",
                "install_date": "not available from local image metadata",
                "rollback_path": f"docker image ID/digest: {image_digest}" if image_digest != "unavailable" else "unavailable",
                "declared": declared_ref,
                "location": "local image store" if image_digest != "unavailable" else "not available locally",
            }
        )
    return service_rows


def model_rows(settings: backup_restore.Settings) -> tuple[list[dict], str]:
    catalog_path = ROOT / "config/models/models.example.yml"
    catalog = catalog_path.read_text(encoding="utf-8")
    entries = re.split(r"(?m)^  - id: ", catalog)[1:]
    metadata = {}
    for entry in entries:
        model_id = entry.splitlines()[0].strip()
        fields = dict(re.findall(r"(?m)^    ([a-z_]+):\s*(.+)$", entry))
        metadata[model_id] = fields

    installed, status = ollama_models(settings)
    rows = []
    for model in installed:
        name = model.get("name", "unknown")
        catalog_name = name.split(":rollback-", 1)[0]
        source_meta = metadata.get(name, metadata.get(catalog_name, {}))
        rollback_tags = [entry.get("name") for entry in installed if entry.get("name", "").startswith(catalog_name + ":rollback-")]
        rows.append(
            {
                "name": f"Ollama model: {name}",
                "version": name,
                "license": source_meta.get("license", "not checked"),
                "source": source_meta.get("source", "not checked")
                + (f" (licence: {source_meta['license_source']})" if source_meta.get("license_source") else ""),
                "checksum": model.get("digest", "not reported by Ollama"),
                "dependencies": "Ollama runtime",
                "install_date": model.get("modified_at", "not reported by Ollama"),
                "rollback_path": ", ".join(rollback_tags) if rollback_tags else "no rollback tag recorded",
                "declared": name,
                "location": "Ollama model store",
            }
        )

    runtime_values = {**settings.env, **os.environ}
    embedding = runtime_values.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-base")
    cache = model_cache(settings)
    model_ref = cache.get(embedding, "not queried in local cache")
    e5_meta = metadata.get(embedding, {})
    rows.append(
        {
            "name": f"Embedding model: {embedding}",
            "version": model_ref,
            "license": e5_meta.get("license", "not checked"),
            "source": e5_meta.get("source", f"https://huggingface.co/{embedding}")
            + (f" (licence: {e5_meta['license_source']})" if e5_meta.get("license_source") else ""),
            "checksum": "unavailable (model file SHA-256 not recorded)",
            "dependencies": "sentence-transformers in the api and ingestion images",
            "install_date": "not recorded by Hugging Face cache metadata",
            "rollback_path": "pre-change backup plus previous api/ingestion image digests",
            "declared": embedding,
            "location": "api image cache",
        }
    )

    stt = runtime_values.get("VOICE_STT_MODEL", "large-v3-turbo")
    rows.append(
        {
            "name": f"Voice STT model: {stt}",
            "version": cache.get("Systran/faster-whisper-" + stt, "not queried in local cache"),
            "license": "not checked",
            "source": f"https://huggingface.co/Systran/faster-whisper-{stt}",
            "checksum": "unavailable",
            "dependencies": "faster-whisper in the voice image",
            "install_date": "not recorded",
            "rollback_path": "previous voice image digest",
            "declared": stt,
            "location": "voice image cache when voice is enabled",
        }
    )
    voices = runtime_values.get("VOICE_TTS_VOICES", "hu_HU-imre-medium,hu_HU-anna-medium,hu_HU-berta-medium")
    voice_names = [item.strip() for item in voices.split(",") if item.strip()]
    piper_files = piper_model_files(settings, voice_names)
    for voice in voice_names:
        voice_files = piper_files.get(voice, {})
        checksum = "; ".join(
            f"{suffix} SHA-256 {voice_files[suffix]}" for suffix in ("onnx", "onnx.json") if voice_files.get(suffix)
        ) or "unavailable"
        rows.append(
            {
                "name": f"Voice TTS model: {voice}",
                "version": "local model files present" if voice_files.get("onnx") else "configured model name",
                "license": voice_files.get("license", "not checked"),
                "source": "https://huggingface.co/rhasspy/piper-voices/tree/main/hu/hu_HU",
                "checksum": checksum,
                "dependencies": "piper-tts in the voice image",
                "install_date": "not recorded",
                "rollback_path": "previous voice image digest",
                "declared": voice,
                "location": "voice image cache when voice is enabled",
            }
        )
    configured_names = [
        re.search(r"(?m)^\s*default_chat_model:\s*(\S+)", catalog),
        re.search(r"(?m)^\s*fallback_cpu_model:\s*(\S+)", catalog),
    ]
    configured_names = [match.group(1) for match in configured_names if match]
    installed_names = {item.get("name") for item in installed}
    runtime_chat = runtime_values.get("OLLAMA_MODEL", configured_names[0] if configured_names else "unknown")
    for name in dict.fromkeys([runtime_chat] + configured_names):
        if name in installed_names:
            continue
        source_meta = metadata.get(name, {})
        rows.append(
            {
                "name": f"Ollama model: {name}",
                "version": name,
                "license": source_meta.get("license", "not checked"),
                "source": source_meta.get("source", "not checked")
                + (f" (licence: {source_meta['license_source']})" if source_meta.get("license_source") else ""),
                "checksum": "unavailable",
                "dependencies": "Ollama runtime",
                "install_date": "not installed or Ollama API unavailable",
                "rollback_path": "none recorded",
                "declared": name,
                "location": status,
            }
        )
    return rows, status


def render(settings: backup_restore.Settings) -> str:
    models, ollama_status = model_rows(settings)
    packages = installed_packages(settings)
    images = image_rows(settings)
    records_path = settings.data_dir / "components" / "model-update-records.json"
    try:
        updates = json.loads(records_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        updates = []

    def row(item: dict) -> str:
        fields = [item.get(name, "not recorded") for name in (
            "name", "version", "license", "source", "checksum", "dependencies", "install_date", "rollback_path"
        )]
        return "| " + " | ".join(str(value).replace("|", "/").replace("\n", " ") for value in fields) + " |"

    lines = [
        "# Component inventory",
        "",
        f"Generated: {datetime.now(UTC).isoformat()}",
        "",
        "This report is gathered from local service metadata, installed Python package metadata and the local image store. "
        "It does not pull, install, or contact external services. `Not checked` and `unavailable` values are intentionally not inferred.",
        f"Ollama inventory: {ollama_status}.",
        "",
        "## Models and images",
        "",
        "| Component | Version | Licence | Source | Checksum | Dependencies | Install date | Rollback path |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(row(item) for item in models + images)
    lines += ["", "## Python dependencies", "", "Declared constraints are shown alongside the installed version. Licences and sources come from package metadata; missing metadata is reported as unchecked.", ""]
    lines += [
        "| Package | Declared | Installed | Licence | Source | Checksum | Dependencies | Install date | Rollback path | Service/location |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in packages:
        values = [item[name] for name in ("name", "declared", "version", "license", "source", "checksum", "dependencies", "install_date", "rollback_path", "location")]
        lines.append("| " + " | ".join(str(value).replace("|", "/").replace("\n", " ") for value in values) + " |")
    lines += ["", "## Model update records", ""]
    if updates:
        lines += ["| Model | Version | Licence | Source | Checksum | Dependencies | Install date | Rollback path |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for item in updates:
            lines.append(row({**item, "name": item.get("model", "unknown")}))
    else:
        lines.append("No model update records have been written yet. The next `make model-update` run records each installed digest and its local rollback tag.")
    lines += ["", "Run `make inventory` again after an approved update to refresh installed digests and image IDs.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--env-file", type=Path, default=None)
    args = parser.parse_args()
    settings = backup_restore.Settings(args.env_file)
    report = render(settings)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        print(f"Inventory written: {args.output}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
