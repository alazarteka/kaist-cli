from __future__ import annotations

from kaist_cli import klms


def test_infer_course_id_for_download_prefers_subdir() -> None:
    course_id = klms._infer_course_id_for_download(  # type: ignore[attr-defined]
        "https://klms.kaist.ac.kr/mod/resource/view.php?id=1205160",
        "2026-spring/180871",
    )
    assert course_id == "180871"


def test_infer_course_id_for_download_from_course_url() -> None:
    course_id = klms._infer_course_id_for_download(  # type: ignore[attr-defined]
        "https://klms.kaist.ac.kr/course/view.php?id=180871",
        None,
    )
    assert course_id == "180871"


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
    names = klms._extract_professors_from_course_page(html)  # type: ignore[attr-defined]
    assert names == ["Sunghee Choi"]


def test_render_course_metadata_markdown_contains_required_fields() -> None:
    text = klms._render_course_metadata_markdown(  # type: ignore[attr-defined]
        course_id="180871",
        course_name="Introduction to Algorithms",
        semester="2026 Spring",
        course_code="CS371_2026_1",
        course_code_base="CS371",
        professors=["Sunghee Choi"],
        course_url="https://klms.kaist.ac.kr/course/view.php?id=180871",
    )
    assert "# Course Metadata" in text
    assert "Course ID: `180871`" in text
    assert "Semester: 2026 Spring" in text
    assert "Professors: Sunghee Choi" in text
