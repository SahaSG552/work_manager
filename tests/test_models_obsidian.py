"""Tests for models (auto_tags, generate_tags) and obsidian pure functions."""

import pytest
from app.models import ParsedOrder, OrderStatus, generate_tags
from app.obsidian import (
    _build_frontmatter,
    _build_attachment_dir_path,
    _sanitize_filename,
)


class TestAutoTags:
    def test_shtapik_tag(self):
        p = ParsedOrder(order_code="ЧМ-1102", owner="KK", description="", raw_text="", edge_type="штапик")
        assert "Штапик" in p.auto_tags

    def test_galtel_tag(self):
        p = ParsedOrder(order_code="ЧМ-1102", owner="KK", description="", raw_text="", edge_type="галтель")
        assert "Галтель" in p.auto_tags

    def test_bort_tag(self):
        p = ParsedOrder(order_code="ЧМ-1102", owner="KK", description="", raw_text="", edge_type="борт 35")
        assert "Борт" in p.auto_tags

    def test_bort_shtapik_tags(self):
        """'борт-штапик' gets both Борт and Штапик? No — 'штапик' is in edge_type so just Штапик."""
        p = ParsedOrder(order_code="ЧМ-1102", owner="KK", description="", raw_text="", edge_type="борт-штапик")
        assert "Штапик" in p.auto_tags
        # "борт" is in edge_type but "штапик" IS also in edge_type → no Борт tag
        assert "Борт" not in p.auto_tags

    def test_email_reference_tag(self):
        p = ParsedOrder(order_code="ЧМ-1102", owner="KK", description="", raw_text="", email_reference=True)
        assert "почта" in p.auto_tags

    def test_no_tags(self):
        p = ParsedOrder(order_code="ЧМ-1102", owner="KK", description="", raw_text="")
        assert p.auto_tags == []


class TestFrontmatter:
    def test_task_tag_always_present(self):
        p = ParsedOrder(order_code="ЧМ-1102", owner="KK", description="test", raw_text="test")
        fm = _build_frontmatter(p, OrderStatus.PENDING)
        assert "task" in fm["tags"]

    def test_thickness_T_field(self):
        p = ParsedOrder(order_code="ЧМ-1102", owner="KK", description="test", raw_text="test", thickness="45 мм")
        fm = _build_frontmatter(p, OrderStatus.IN_PROGRESS)
        assert fm["T"] == 45
        assert fm["thickness"] == "45 мм"

    def test_thickness_T_no_unit(self):
        p = ParsedOrder(order_code="ЧМ-1102", owner="KK", description="test", raw_text="test", thickness="16")
        fm = _build_frontmatter(p, OrderStatus.IN_PROGRESS)
        assert fm["T"] == 16

    def test_projects_frontmatter(self):
        p = ParsedOrder(order_code="ИП-1116", owner="KK", description="test", raw_text="test")
        fm = _build_frontmatter(p, OrderStatus.PENDING, projects=["[[ИП-1115]]"])
        assert fm["projects"] == ["[[ИП-1115]]"]

    def test_no_projects_key_when_none(self):
        p = ParsedOrder(order_code="ЧМ-1102", owner="KK", description="test", raw_text="test")
        fm = _build_frontmatter(p, OrderStatus.PENDING)
        assert "projects" not in fm

    def test_email_reference_field(self):
        p = ParsedOrder(order_code="ЧМ-1102", owner="KK", description="test", raw_text="test", email_reference=True)
        fm = _build_frontmatter(p, OrderStatus.PENDING)
        assert fm["emailReference"] is True

    def test_status_in_frontmatter(self):
        p = ParsedOrder(order_code="ЧМ-1102", owner="KK", description="test", raw_text="test")
        fm = _build_frontmatter(p, OrderStatus.URGENT)
        assert fm["status"] == "urgent"

    def test_no_empty_thickness_T(self):
        p = ParsedOrder(order_code="ЧМ-1102", owner="KK", description="test", raw_text="test", thickness=None)
        fm = _build_frontmatter(p, OrderStatus.PENDING)
        assert "T" not in fm
        assert "thickness" not in fm


class TestAttachmentDirPath:
    def test_path_format(self):
        """Path should be ДАНАТА/{YYYY}/{MM} {MonthName}/{owner}/- {ordername}."""
        p = ParsedOrder(order_code="ЧМ-1102", owner="KK", description="test", raw_text="test")
        path = _build_attachment_dir_path(p)
        assert path.startswith("ДАНАТА/")
        assert "/KK/- ЧМ-1102" in path

    def test_folder_name_override(self):
        p = ParsedOrder(order_code="ИП-1115", owner="KK", description="test", raw_text="test")
        path = _build_attachment_dir_path(p, folder_name="ИП-1115, 1116")
        assert "/KK/- ИП-1115, 1116" in path

    def test_month_name_english(self):
        p = ParsedOrder(order_code="ЧМ-1102", owner="KK", description="test", raw_text="test")
        path = _build_attachment_dir_path(p)
        # Should contain English month name
        import re
        months = ["January", "February", "March", "April", "May", "June",
                   "July", "August", "September", "October", "November", "December"]
        assert any(m in path for m in months)


class TestSanitizeFilename:
    def test_removes_invalid_chars(self):
        assert "<" not in _sanitize_filename('test<file>.txt')
        assert ">" not in _sanitize_filename('test>file.txt')
        assert ":" not in _sanitize_filename('test:file.txt')
        assert '"' not in _sanitize_filename('test"file.txt')

    def test_replaces_with_dash(self):
        result = _sanitize_filename('test:file?.txt')
        assert ":" not in result
        assert "?" not in result

    def test_normal_name_unchanged(self):
        assert _sanitize_filename("ЧМ-1102") == "ЧМ-1102"
