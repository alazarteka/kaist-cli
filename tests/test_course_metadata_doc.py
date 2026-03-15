from __future__ import annotations

from kaist_cli.v2.klms.courses import _extract_professors_from_course_page
from kaist_cli.v2.klms.models import Course


def test_extract_professors_from_course_page_block() -> None:
    html = """
    <div class="border-left py-2">
      <div>Professors</div>
      <div class="dropdown d-inline-block">
        <a class="dropdown-toggle">Sunghee Choi</a>
        <a href="mailto:sunghee@kaist.ac.kr">sunghee@kaist.ac.kr</a>
      </div>
      <div>Assistants Kyounga Woo</div>
    </div>
    """
    names = _extract_professors_from_course_page(html)
    assert names == ("Sunghee Choi", "sunghee@kaist.ac.kr")


def test_course_model_serialization_contains_professors() -> None:
    payload = Course(
        id="180871",
        title="Introduction to Algorithms",
        url="https://klms.kaist.ac.kr/course/view.php?id=180871",
        course_code="CS371_2026_1",
        course_code_base="CS371",
        term_label="2026 Spring",
        professors=("Sunghee Choi",),
        source="html:course",
        confidence=0.9,
    )
    rendered = payload.to_dict()
    assert rendered["id"] == "180871"
    assert rendered["course_code_base"] == "CS371"
    assert rendered["professors"] == ["Sunghee Choi"]
