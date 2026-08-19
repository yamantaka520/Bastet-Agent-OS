

def test_model3d_is_a_first_class_media_kind():
    """Meshy-style 3D generation was filed under "image" for lack of a truer
    category — misleading the browser, the grants view, and the media brief."""
    from bastet_agent_os.orchestrator import Orchestrator
    from bastet_agent_os.resource_kinds import BY_ID, validate

    kind = BY_ID["model3d"]
    assert kind["group"] == "media" and kind["auth"] == "required"
    assert "model3d" in Orchestrator.MEDIA_KINDS   # media brief covers it
    # same endpoint hygiene as every media kind
    problems = validate("model3d", "https://api.meshy.ai/v1/chat/completions",
                        "secret:k1", {"default_model": "meshy-5"})
    assert "endpoint-is-operation-url" in problems
    assert validate("model3d", "https://api.meshy.ai", "secret:k1", {}) == []
