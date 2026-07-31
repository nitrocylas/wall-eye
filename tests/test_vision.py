"""Unit tests for the pure helpers in vision.py (item filtering + box normalization)."""
import vision


class TestGoodItem:
    def test_accepts_real_object_names(self):
        assert vision._good_item("jacket on the bed")
        assert vision._good_item("cup")

    def test_rejects_repetition_spam(self):
        assert not vision._good_item(".")
        assert not vision._good_item("...")

    def test_rejects_coordinate_leak(self):
        assert not vision._good_item("[0,329,487,716]")
        assert not vision._good_item("(0.1, 0.2, 0.3, 0.4)")

    def test_rejects_path_like_hallucinations(self):
        assert not vision._good_item("./home/room-1")
        assert not vision._good_item("a\\b")

    def test_rejects_non_strings_and_empty(self):
        assert not vision._good_item(123)
        assert not vision._good_item("")


class TestNormBox:
    def test_pixels_to_fraction(self):
        # 640x480 frame, pixel corners -> fractional [x, y, w, h]
        assert vision._norm_box([64, 48, 128, 96], 640, 480,
                                "pixel") == [0.1, 0.1, 0.1, 0.1]

    def test_corner_order_does_not_matter(self):
        forwards = vision._norm_box([64, 48, 128, 96], 640, 480, "pixel")
        reversed_ = vision._norm_box([128, 96, 64, 48], 640, 480, "pixel")
        assert forwards == reversed_

    def test_rejects_wrong_length(self):
        assert vision._norm_box([1, 2, 3], 640, 480, "pixel") is None

    def test_rejects_non_list(self):
        assert vision._norm_box("nope", 640, 480, "pixel") is None

    def test_rejects_degenerate_zero_area_box(self):
        # all-equal pixel corners -> zero width/height -> unusable
        assert vision._norm_box([100, 100, 100, 100], 640, 480, "pixel") is None

    def test_fractional_box_passes_through(self):
        assert vision._norm_box([0.1, 0.1, 0.2, 0.2], 640, 480,
                                "frac") == [0.1, 0.1, 0.1, 0.1]

    def test_norm1000_box_scaled_by_1000_not_frame(self):
        got = vision._norm_box([0, 367, 458, 691], 512, 288, "norm1000")
        assert got == [0.0, 0.367, 0.458, 0.324]

    def test_oversized_junk_clamps_instead_of_crashing(self):
        got = vision._norm_box([0, 0, 5000, 5000], 640, 480, "pixel")
        assert got == [0.0, 0.0, 1.0, 1.0]


class TestDetectBoxSpace:
    # A typical watcher frame (1280x720 downscaled to 512 wide).
    W, H = 512, 288

    def test_fractions_detected(self):
        assert vision._detect_box_space(
            [[0.1, 0.2, 0.4, 0.5]], self.W, self.H) == "frac"

    def test_overflow_evidence_means_norm1000_for_any_model(self):
        # y=691 can't be a pixel of a 288-tall frame but fits in 1000
        assert vision._detect_box_space(
            [[0, 367, 458, 691]], self.W, self.H,
            prefer_norm1000=False) == "norm1000"

    def test_ambiguous_defaults_to_pixels_for_pixel_models(self):
        # everything fits the frame -> qwen2.5vl-style models mean real pixels
        assert vision._detect_box_space(
            [[64, 48, 128, 96]], self.W, self.H,
            prefer_norm1000=False) == "pixel"

    def test_ambiguous_defaults_to_norm1000_for_norm1000_models(self):
        # a 0-1000 box near the top-left also fits inside 512x288 as pixels;
        # the model hint must break the tie or the rectangle lands on the
        # wrong thing
        assert vision._detect_box_space(
            [[104, 236, 358, 279]], self.W, self.H,
            prefer_norm1000=True) == "norm1000"

    def test_one_response_never_mixes_spaces(self):
        # box 1 overflows the frame (definitive 0-1000); box 2
        # [416,184,548,368] must be read in the SAME space even for a model
        # with no norm-1000 preference
        boxes = [[104, 236, 358, 479], [416, 184, 548, 368]]
        space = vision._detect_box_space(boxes, self.W, self.H,
                                         prefer_norm1000=False)
        assert space == "norm1000"
        got = vision._norm_box(boxes[1], self.W, self.H, space)
        assert got == [0.416, 0.184, 0.132, 0.184]

    def test_oversized_junk_falls_back_to_pixels(self):
        assert vision._detect_box_space(
            [[0, 0, 5000, 5000]], self.W, self.H,
            prefer_norm1000=True) == "pixel"

    def test_empty_or_malformed_defaults_to_pixels(self):
        assert vision._detect_box_space([], self.W, self.H) == "pixel"
        assert vision._detect_box_space([None, "junk", [1, 2]],
                                        self.W, self.H) == "pixel"

    def test_norm1000_model_hints(self):
        # qwen3-vl grounds in 0-1000 space; qwen2.5vl grounds in real
        # pixels and must stay on the pixel default
        assert vision._prefers_norm1000("qwen3-vl:8b-instruct")
        assert not vision._prefers_norm1000("qwen2.5vl:3b")
        assert not vision._prefers_norm1000("")
        assert not vision._prefers_norm1000(None)


class TestCleanRegions:
    W, H = 512, 288

    def test_normalizes_good_regions(self):
        # a typical qwen3-vl grounding reply (0-1000 space, y hits 1000)
        raw = [{"label": "clothes on the bed", "box": [67, 579, 480, 1000]},
               {"label": "chair with items", "box": [462, 135, 602, 500]}]
        out = vision._clean_regions(raw, self.W, self.H, prefer_norm1000=True)
        assert [r["label"] for r in out] == ["clothes on the bed",
                                             "chair with items"]
        assert out[0]["box"] == [0.067, 0.579, 0.413, 0.421]

    def test_drops_junk_labels_and_bad_boxes(self):
        raw = [{"label": ".", "box": [0, 0, 500, 500]},
               {"label": "cup", "box": [1, 2, 3]},          # wrong length
               {"label": "sock", "box": [100, 100, 100, 100]},  # zero area
               "not-a-dict", None]
        assert vision._clean_regions(raw, self.W, self.H) == []

    def test_junk_labelled_box_still_carries_scale_evidence(self):
        # the junk-labelled box overflows the frame (norm-1000 proof); the kept
        # box fits either way and must be read in the SAME space
        raw = [{"label": ".", "box": [0, 0, 900, 900]},
               {"label": "sock", "box": [100, 100, 200, 200]}]
        out = vision._clean_regions(raw, self.W, self.H)
        assert out == [{"label": "sock", "box": [0.1, 0.1, 0.1, 0.1]}]

    def test_non_list_input_is_empty(self):
        assert vision._clean_regions(None, self.W, self.H) == []
        assert vision._clean_regions("junk", self.W, self.H) == []


class TestWithPlace:
    def test_appends_missing_place(self):
        assert vision._with_place("white bag", "floor") == \
            "white bag on the floor"

    def test_keeps_phrase_that_already_names_the_place(self):
        assert vision._with_place("clothes on the bed", "bed") == \
            "clothes on the bed"

    def test_junk_place_leaves_phrase_alone(self):
        assert vision._with_place("cup", "desk / dresser / shelves") == "cup"
        assert vision._with_place("cup", "") == "cup"

    def test_two_word_place_is_fine(self):
        assert vision._with_place("towel", "desk chair") == \
            "towel on the desk chair"


class TestMergeAreaItems:
    def test_area_finds_extend_the_flat_list(self):
        areas = [{"area": "floor", "out_of_place": ["white bag"]},
                 {"area": "chairs", "out_of_place": ["items piled up"]},
                 {"area": "desk", "out_of_place": []}]
        got = vision._merge_area_items(["clothes on the bed"], areas)
        assert got == ["clothes on the bed", "white bag on the floor",
                       "items piled up on the chairs"]

    def test_same_mess_in_both_lists_is_not_duplicated(self):
        areas = [{"area": "floor", "out_of_place": ["white bag"]}]
        got = vision._merge_area_items(["white bag on the floor"], areas)
        assert got == ["white bag on the floor"]

    def test_same_object_on_different_surfaces_is_two_messes(self):
        areas = [{"area": "bed", "out_of_place": ["clothes"]},
                 {"area": "chair", "out_of_place": ["clothes"]}]
        got = vision._merge_area_items([], areas)
        assert got == ["clothes on the bed", "clothes on the chair"]

    def test_junk_and_malformed_areas_are_ignored(self):
        areas = [None, "junk", {"area": "floor"},
                 {"area": "floor", "out_of_place": "not-a-list"},
                 {"area": "floor", "out_of_place": [".", "[1,2,3,4]", 7]}]
        assert vision._merge_area_items(["cup on desk"], areas) == \
            ["cup on desk"]

    def test_cap_holds(self):
        things = ["sock", "shoe", "cup", "plate", "jacket", "towel", "book",
                  "wrapper", "bottle", "cable", "hat", "glove"]
        areas = [{"area": "floor", "out_of_place": things}]
        assert len(vision._merge_area_items([], areas)) == 10

    def test_phrases_differing_only_by_number_are_duplicates(self):
        # digits are not words - 'box 1' and 'box 2' fingerprint the same,
        # which stops a degenerate numbered-list response flooding the alert
        areas = [{"area": "floor",
                  "out_of_place": [f"box {i}" for i in range(5)]}]
        assert vision._merge_area_items([], areas) == ["box 0 on the floor"]


class _FakeResp:
    def __init__(self, status_code=200, content='{"regions": []}'):
        self.status_code = status_code
        self._content = content

    def json(self):
        return {"message": {"content": self._content}}


class TestGroundItems:
    def _call(self, monkeypatch, resp=None, exc=None):
        def fake_post(url, json=None, timeout=None):
            if exc:
                raise exc
            return resp
        monkeypatch.setattr(vision.requests, "post", fake_post)
        return vision._ground_items(["clothes on the bed"], "img-b64",
                                    512, 288, "http://x", "qwen3-vl:8b-instruct",
                                    2048, 30)

    def test_returns_raw_regions(self, monkeypatch):
        resp = _FakeResp(content='{"regions": [{"label": "clothes", '
                                 '"box": [67, 579, 480, 1000]}]}')
        got = self._call(monkeypatch, resp=resp)
        assert got == [{"label": "clothes", "box": [67, 579, 480, 1000]}]

    def test_fails_open_on_http_error(self, monkeypatch):
        assert self._call(monkeypatch, resp=_FakeResp(status_code=500)) is None

    def test_fails_open_on_network_error(self, monkeypatch):
        import requests
        assert self._call(monkeypatch,
                          exc=requests.ConnectionError("down")) is None

    def test_fails_open_on_bad_json(self, monkeypatch):
        assert self._call(monkeypatch,
                          resp=_FakeResp(content="not json")) is None

    def test_fails_open_on_non_list_regions(self, monkeypatch):
        assert self._call(monkeypatch,
                          resp=_FakeResp(content='{"regions": "junk"}')) is None


class TestCropRect:
    def test_pads_and_converts_to_pixels(self):
        # centred 20%x20% box in 1000x1000, 15% pad -> 3% of frame each side
        left, top, right, bottom = vision._crop_rect(
            [0.4, 0.4, 0.2, 0.2], 1000, 1000)
        assert (left, top, right, bottom) == (370, 370, 630, 630)

    def test_clamps_padding_at_frame_edges(self):
        rect = vision._crop_rect([0.0, 0.0, 0.5, 0.5], 1000, 1000)
        assert rect[0] == 0 and rect[1] == 0

    def test_rejects_sliver_too_small_to_judge(self):
        assert vision._crop_rect([0.5, 0.5, 0.001, 0.001], 640, 480) is None


class TestLabelMatchesItem:
    def test_shared_object_word_matches(self):
        assert vision._label_matches_item("jacket", "jacket on the bed")

    def test_location_words_alone_do_not_match(self):
        # "floor" is a location, not the object - must not link these
        assert not vision._label_matches_item("box on the floor",
                                              "sock on the floor")

    def test_unrelated_labels_do_not_match(self):
        assert not vision._label_matches_item("cardboard box", "mug on desk")


class TestApplyVerification:
    def _verdict(self, **over):
        base = {"alert": True, "summary": "Clothes on the bed.",
                "items": ["clothes on the bed"], "confidence": "medium",
                "regions": [{"label": "clothes", "box": [0.1, 0.1, 0.2, 0.2]}],
                "vetoed": []}
        base.update(over)
        return base

    def test_confirmed_region_is_kept(self):
        v = {"seen": "grey hoodie", "is_person_or_pet": False,
             "is_part_of_room": False, "out_of_place": True}
        out = vision.apply_verification(
            self._verdict(), [(self._verdict()["regions"][0], v)])
        assert out["alert"] and out["items"] == ["clothes on the bed"]
        # a confirmed, more specific 'seen' refines the drawn label
        assert out["regions"][0]["label"] == "grey hoodie"

    def test_bedding_mistaken_for_clothes_is_vetoed_and_alert_cancelled(self):
        v = {"seen": "folded duvet", "is_person_or_pet": False,
             "is_part_of_room": True, "out_of_place": False}
        out = vision.apply_verification(
            self._verdict(), [(self._verdict()["regions"][0], v)])
        assert out["vetoed"] == ["clothes"]
        assert out["regions"] == [] and out["items"] == []
        assert out["alert"] is False
        assert "close-up" in out["summary"]

    def test_person_detection_is_vetoed(self):
        v = {"seen": "a person's arm", "is_person_or_pet": True,
             "is_part_of_room": False, "out_of_place": True}
        out = vision.apply_verification(
            self._verdict(), [(self._verdict()["regions"][0], v)])
        assert out["regions"] == [] and out["alert"] is False

    def test_failed_verification_fails_open(self):
        # a broken second pass must never silence a real alert
        out = vision.apply_verification(
            self._verdict(), [(self._verdict()["regions"][0], None)])
        assert out["alert"] and out["regions"] and out["items"]

    def test_partial_veto_keeps_alert_and_other_items(self):
        verdict = self._verdict(
            items=["clothes on the bed", "box on the floor"],
            regions=[{"label": "clothes", "box": [0.1, 0.1, 0.2, 0.2]},
                     {"label": "box", "box": [0.5, 0.5, 0.2, 0.2]}])
        veto = {"seen": "duvet", "is_person_or_pet": False,
                "is_part_of_room": True, "out_of_place": False}
        keep = {"seen": "cardboard box", "is_person_or_pet": False,
                "is_part_of_room": False, "out_of_place": True}
        out = vision.apply_verification(
            verdict, [(verdict["regions"][0], veto),
                      (verdict["regions"][1], keep)])
        assert out["alert"] is True
        assert out["items"] == ["box on the floor"]
        assert [r["label"] for r in out["regions"]] == ["cardboard box"]

    def test_junk_seen_string_does_not_replace_label(self):
        v = {"seen": "[0,1,2,3]", "is_person_or_pet": False,
             "is_part_of_room": False, "out_of_place": True}
        out = vision.apply_verification(
            self._verdict(), [(self._verdict()["regions"][0], v)])
        assert out["regions"][0]["label"] == "clothes"

    def test_verify_schema_describes_before_judging(self):
        # Ollama emits properties in ALPHABETICAL order (not declared order),
        # so the contract is on the sorted names: describe first, judge last
        keys = sorted(vision.VERIFY_SCHEMA["properties"])
        assert keys[0] == "described"
        assert keys.index("described") < keys.index("out_of_place")

    def test_verdict_schema_evidence_sorts_before_verdict(self):
        # alphabetical fill order must be: sweep areas -> items -> boxes ->
        # summary -> verdict; a rename that breaks this silently reintroduces
        # the model committing to 'alert' before looking
        keys = sorted(vision.VERDICT_SCHEMA["properties"])
        assert keys == ["areas", "items", "regions", "summary",
                        "verdict_alert", "verdict_confidence"]
        sub = sorted(vision.VERDICT_SCHEMA["properties"]["areas"]
                     ["items"]["properties"])
        assert sub == ["area", "out_of_place"]
