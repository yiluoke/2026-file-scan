"""DB・Embeddingに依存しない接続試験専用の固定応答。"""

TEST_CONTENT = "これは検索APIのテスト応答です。DB検索は実行していません。"


def fixed_response(request_id, config_version):
    return {
        "api_version": "1",
        "request_id": request_id,
        "records": [
            {
                "content": TEST_CONTENT,
                "score": 1.0,
                "title": "検索APIテスト",
                "metadata": {"test_mode": True, "evidence_role": "test"},
            }
        ],
        "retrieval_status": {
            "snapshot_id": None,
            "search_mode": "current",
            "resolved_release_id": None,
            "catalog_revision": None,
            "publication_revision": None,
            "policy_revision": None,
            "access_control": "not_enforced",
            "execution_mode": "test",
            "embedding_profile_id": None,
            "search_config_version": config_version,
            "coverage_scope": "test",
            "coverage_state": "unknown",
            "pending_updates": None,
            "failed_documents": None,
            "partial_documents": None,
            "uncollected_documents": None,
            "index_pending_documents": None,
            "unknown_documents": None,
            "result_state": "matched",
            "returned_records": 1,
            "limit_reasons": [],
            "message": TEST_CONTENT,
        },
    }
