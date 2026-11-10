"""Foreign titles stay out of the post. They only feed a local angle."""

from app.prompts import build_brainstorm_prompt, build_post_prompt
from app.sparks import titles_from_listing


def test_listing_keeps_ordinary_titles() -> None:
    payload = {
        "data": {
            "children": [
                {"data": {"title": "short"}},
                {"data": {"title": "My neighbour keeps score of who said hello first and I think I am losing", "stickied": True}},
                {"data": {"over_18": True, "title": "A long enough title about something people argue about at dinner"}},
                {"data": {"title": "Breaking: election news from the capital today and what it means"}},
                {"data": {"title": "People here pretend a cheap watch is a personality and nobody calls it out"}},
            ]
        }
    }
    assert titles_from_listing(payload) == [
        "People here pretend a cheap watch is a personality and nobody calls it out"
    ]


def test_brainstorm_moves_a_foreign_line_home() -> None:
    prompt = build_brainstorm_prompt(
        "статус",
        "Бразилія: People here pretend a cheap watch is a personality",
        ["старий хід"],
    )
    assert "не сюжет" in prompt
    assert "українське життя" in prompt
    assert "старий хід" in prompt
    assert "Не переклад" in prompt


def test_post_uses_the_local_angle_and_hides_the_source() -> None:
    prompt = build_post_prompt("статус", [], spark="дешевий годинник, яким людина тримає статус")
    assert "дешевий годинник" in prompt
    assert "Не згадуй іншу країну" in prompt
    post_with_idea = build_post_prompt("статус", [], idea="моя думка", spark="чуже")
    assert "чуже" not in post_with_idea
