from app.cli import build_parser


def test_reindex_document_cli_accepts_optional_force() -> None:
    parser = build_parser()

    default = parser.parse_args(["reindex-document", "doc-1"])
    forced = parser.parse_args(["reindex-document", "doc-1", "--force"])

    assert default.command == "reindex-document"
    assert default.document_id == "doc-1"
    assert default.force is False
    assert forced.document_id == "doc-1"
    assert forced.force is True