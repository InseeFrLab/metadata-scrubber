"""Versions DDI, agence et BasedOnObject des CodeLists."""

import json

from metadata_scrubber.extractor import extract_codelists, latest_versions, parse_xml
from metadata_scrubber.types import CodeList

XML_VERSIONS = b"""<?xml version="1.0" encoding="UTF-8"?>
<MetaDataDocument xmlns:r="ddi:reusable:3_3">
  <Fragment><CodeList>
    <r:Agency>fr.insee</r:Agency><r:ID>cl-1</r:ID><r:Version>5</r:Version>
    <CodeListName><r:String>L_TYPEQU_2024</r:String></CodeListName>
    <Code><r:Value>1</r:Value></Code>
  </CodeList></Fragment>
  <Fragment><CodeList>
    <r:Agency>fr.insee</r:Agency><r:ID>cl-2</r:ID><r:Version>1</r:Version>
    <r:BasedOnObject>
      <r:BasedOnReference>
        <r:Agency>fr.insee</r:Agency><r:ID>cl-1</r:ID><r:Version>5</r:Version>
        <r:TypeOfObject>CodeList</r:TypeOfObject>
      </r:BasedOnReference>
    </r:BasedOnObject>
    <CodeListName><r:String>L_TYPEQU_2024_DIFF</r:String></CodeListName>
    <Code><r:Value>1</r:Value></Code>
  </CodeList></Fragment>
  <Fragment><CodeList>
    <r:Agency>fr.insee</r:Agency><r:ID>cl-1</r:ID><r:Version>12</r:Version>
    <CodeListName><r:String>L_TYPEQU_2025</r:String></CodeListName>
    <Code><r:Value>1</r:Value></Code>
  </CodeList></Fragment>
</MetaDataDocument>
"""


def test_extracts_agency_version_and_based_on():
    cls = {cl.id: cl for cl in extract_codelists(parse_xml(XML_VERSIONS))}
    assert cls["cl-2"].agency == "fr.insee"
    assert cls["cl-2"].version == "1"
    assert cls["cl-2"].based_on == ["cl-1"]
    assert cls["cl-1"].based_on == []


def test_keeps_latest_version_at_first_position():
    cls = extract_codelists(parse_xml(XML_VERSIONS))
    assert [cl.id for cl in cls] == ["cl-1", "cl-2"]
    # Version 12 (numérique) plus récente que 5, et pas « 5 » > « 12 » en texte
    assert cls[0].version == "12" and cls[0].name == "L_TYPEQU_2025"


def test_latest_versions_without_version():
    a = CodeList(id="a", name="v?", label="")
    b = CodeList(id="a", name="v2", label="", version="2")
    assert latest_versions([a, b])[0].name == "v2"


def test_registry_carries_ddi_reference(tmp_path):
    from metadata_scrubber import main

    xml = tmp_path / "in.xml"
    xml.write_bytes(XML_VERSIONS)
    result = main.run_pipeline(xml_source=str(xml), audit_dir=str(tmp_path / "out"), run_llm=False)

    run_dir = tmp_path / "out" / result["run_id"]
    registry = json.loads((run_dir / "codelist_duplicates.json").read_text(encoding="utf-8"))
    dup = registry["cl-1"]["duplicates"][0]
    assert (registry["cl-1"]["agency"], registry["cl-1"]["version"]) == ("fr.insee", "12")
    assert dup["id"] == "cl-2" and dup["based_on"] == ["cl-1"]

    summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["counts"]["older_versions_ignored"] == 1
