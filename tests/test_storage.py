"""Exercise real S3 protocol transfers against a disposable local server."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time

import pytest

from caligo import scheduler
from caligo.common import read_json, transfer
from test_harness import bootstrap


@pytest.mark.skipif(not shutil.which("rclone"), reason="rclone is required for S3 integration")
def test_s3_inputs_publication_and_source_isolation(project, tmp_path, monkeypatch):
    storage = tmp_path / "s3"
    (storage / "input-bucket").mkdir(parents=True)
    (storage / "output-bucket").mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=localhost"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    server = subprocess.Popen(["rclone", "serve", "s3", str(storage), "--addr", "127.0.0.1:%s" % port, "--auth-key", "test-key,test-secret", "--cert", str(cert), "--key", str(key)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            if server.poll() is not None:
                pytest.fail("S3 test server exited")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("S3 test server did not start")
        monkeypatch.setenv("RCLONE_S3_ENDPOINT", "https://127.0.0.1:%s" % port)
        # Trust only this disposable server's certificate for these subprocesses.
        monkeypatch.setenv("RCLONE_NO_CHECK_CERTIFICATE", "true")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "")
        transfer(project / "data", "s3://input-bucket/data")
        path = project / "project.toml"
        text = path.read_text().replace('outputs = "results"', 'outputs = "s3://output-bucket/results"')
        path.write_text(text + '\n[inputs]\ndata = "s3://input-bucket/data"\n')
        c = bootstrap(project, tmp_path, monkeypatch)
        supervisor, user = c.create("supervisor", "setup"), c.create("user", "baseline")
        (Path(supervisor["workspace"]) / "data/values.json").write_text("[9]")
        original = tmp_path / "original"
        transfer("s3://input-bucket/data", original)
        assert json.loads((original / "values.json").read_text()) == [1, 2, 3]
        assert json.loads((Path(user["workspace"]) / "data/values.json").read_text()) == [1, 2, 3]
        monkeypatch.setattr(scheduler, "submit", lambda *args: "42")
        result = c.submit(user["id"], None, "/agent.pyz")
        record = c.run_dir(result["id"])
        (record / "artifacts/partial.txt").write_text("failed run evidence")
        from caligo.common import write_json
        write_json(record / "outcome.json", {"state": "FAILED", "exit_code": 7})
        c.publish(result["id"])
        download = tmp_path / "download"
        transfer("s3://output-bucket/results/" + result["id"], download)
        assert read_json(download / "outcome.json")["exit_code"] == 7
        assert (download / "artifacts/partial.txt").read_text() == "failed run evidence"
        assert (download / "inputs/executable/train.py").is_file()
        assert not (download / "fixed").exists()
        assert read_json(record / "publication.json")["state"] == "PUBLISHED"
    finally:
        server.terminate()
        server.wait(timeout=10)
