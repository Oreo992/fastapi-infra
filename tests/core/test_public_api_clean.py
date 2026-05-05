import infra


def test_top_level_public_api_is_small_and_explicit():
    assert sorted(infra.__all__) == [
        "InfraContext",
        "InfraSettings",
        "PluginSettings",
        "setup_infra",
    ]
