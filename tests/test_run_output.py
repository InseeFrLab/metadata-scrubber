"""Tests du dossier de run daté, des logs et du registre nettoyé stable."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

import pytest

from metadata_scrubber.cleaned_registry import cleaned_registry_path
from metadata_scrubber.registry_io import write_text
from metadata_scrubber.run_output import RunLogCapture, make_run_dir


class TestMakeRunDir:
    def test_format_and_root(self):
        now = datetime(2026, 10, 22, 14, 5, 9, tzinfo=timezone.utc)
        run_id, run_dir = make_run_dir("s3://projet-metadonnees-rmes/scrubber_output/", now)
        assert run_id == "20261022-140509"
        assert run_dir == "s3://projet-metadonnees-rmes/scrubber_output/20261022-140509"

    def test_local_root(self):
        _, run_dir = make_run_dir("/tmp/out", datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
        assert run_dir == "/tmp/out/20260102-030405"


class TestCleanedRegistryPath:
    def test_beside_registry_without_run_folder(self):
        assert (
            cleaned_registry_path("s3://b/scrubber_output/codelist_duplicates.json")
            == "s3://b/scrubber_output/cleaned_codelists.json"
        )

    def test_stays_at_output_root_for_a_run_folder(self):
        assert (
            cleaned_registry_path("s3://b/scrubber_output/20261022-140509/codelist_duplicates.json")
            == "s3://b/scrubber_output/cleaned_codelists.json"
        )

    def test_local_run_folder(self):
        assert (
            cleaned_registry_path("/data/out/20261022-140509/codelist_duplicates.json")
            == "/data/out/cleaned_codelists.json"
        )

    def test_bare_filename(self):
        assert cleaned_registry_path("codelist_duplicates.json") == "cleaned_codelists.json"


class TestWriteText:
    def test_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "a" / "b" / "pipeline.log"
        write_text("héllo", str(target))
        assert target.read_text(encoding="utf-8") == "héllo"


class TestRunLogCapture:
    def test_captures_print_and_logging_and_writes_file(self, tmp_path, capsys):
        log_path = tmp_path / "run" / "pipeline.log"
        with RunLogCapture(str(log_path)):
            print("[1/9] hello")
            logging.getLogger("metadata_scrubber.test").info("from logger")

        content = log_path.read_text(encoding="utf-8")
        assert "[1/9] hello" in content
        assert "from logger" in content
        # La sortie d'origine continue de fonctionner
        assert "[1/9] hello" in capsys.readouterr().out
        # stdout restauré
        assert not hasattr(sys.stdout, "_buffer")

    def test_log_is_written_even_on_error(self, tmp_path):
        log_path = tmp_path / "pipeline.log"
        with pytest.raises(RuntimeError):
            with RunLogCapture(str(log_path)):
                print("avant l'erreur")
                raise RuntimeError("boom")

        content = log_path.read_text(encoding="utf-8")
        assert "avant l'erreur" in content
        assert "RuntimeError: boom" in content


XML_MIN = """<?xml version="1.0" encoding="UTF-8"?>
<DDIInstance xmlns="ddi:instance:3_3" xmlns:r="ddi:reusable:3_3" xmlns:l="ddi:logicalproduct:3_3">
</DDIInstance>
"""


class TestRunPipelineOutputs:
    def test_writes_three_files_in_dated_folder(self, tmp_path, monkeypatch):
        """run_pipeline sur un XML sans CodeList : dossier daté, 3 fichiers, log complet."""
        from metadata_scrubber import main

        xml = tmp_path / "in.xml"
        xml.write_text(XML_MIN, encoding="utf-8")

        result = main.run_pipeline(
            xml_source=str(xml), audit_dir=str(tmp_path / "out"), run_llm=False
        )

        run_dir = tmp_path / "out" / result["run_id"]
        assert result["run_dir"] == str(run_dir)
        assert (run_dir / "codelist_duplicates.json").is_file()
        assert (run_dir / "pipeline.log").is_file()
        assert "[9/9]" in (run_dir / "pipeline.log").read_text(encoding="utf-8")

        summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
        assert summary["status"] == "success"
        assert summary["xml_source"] == str(xml)
        assert any(
            w["phase"] == "semantic" and w["status"] == "disabled" for w in summary["warnings"]
        )
        assert result["warnings"] == summary["warnings"]

    def test_error_still_writes_log_and_summary(self, tmp_path):
        from metadata_scrubber import main

        with pytest.raises(Exception):
            main.run_pipeline(
                xml_source=str(tmp_path / "missing.xml"),
                audit_dir=str(tmp_path / "out"),
                run_llm=False,
            )

        runs = list((tmp_path / "out").iterdir())
        assert len(runs) == 1
        summary = json.loads((runs[0] / "run_summary.json").read_text(encoding="utf-8"))
        assert summary["status"] == "error"
        assert (runs[0] / "pipeline.log").is_file()
