import others_vs_king as mode


def test_clean_action_removes_platform_words():
    assert mode.clean_action("high jumping TikTok reels") == "high jumping"


def test_search_pools_are_separate_and_platform_free():
    normal = mode.action_queries("ski jumping")
    payoff = mode.action_queries("ski jumping", payoff=True)
    assert "ski jumping" in normal
    assert any("insane" in query for query in payoff)
    assert not set(normal) == set(payoff)
    assert all("tiktok" not in query.lower() and "instagram" not in query.lower()
               for query in normal + payoff)


def test_user_action_wins_over_auto_rotation():
    assert mode.choose_action("cliff diving") == "cliff diving"


def test_high_jump_queries_preserve_reference_visual_contract():
    setup = mode.action_queries("high jumping")
    payoff = mode.action_queries("high jumping", payoff=True)
    assert all("track and field" not in query for query in setup)
    assert any("touch" in query for query in setup)
    assert any("dog" in query for query in payoff)
