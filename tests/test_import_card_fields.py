"""Grounded card slots, compact source ownership and creator edit precedence."""
import json
import unittest
from copy import deepcopy

from persona_runtime.character import validate_character
from persona_runtime.import_card_fields import admit_cards, blank_card, build_source, ordinary_preview, validate_edit
from persona_runtime.import_library import FIELDS


def drafts():
    result = {}
    for role, name in (("A", "1"), ("B", "2")):
        evidence = {"id": name, "speaker": role, "start": 0, "end": 6,
                    "quote": "175 cm", "date": None}
        claim = {"id": role * 64, "field":"initial_background", "value":"175", "scope":"100",
                 "kind":"self_report", "evidence":[evidence], "decision":"pending"}
        spec = {"format_version":1,"name":name, "core_traits":[], "values_and_boundaries":[],
                "introduction":"", "expression_style":"", "initial_background":""}
        result[role] = {"spec":spec, "relationship":"", "claims":[claim], "edited_fields":[]}
    return result


def slot(v, *sources):
    return {"value": v, "sources": list(sources)}


class CardFieldCases(unittest.TestCase):
    def test_all_current_observations_and_role_local_refs(self):
        data = drafts()
        source = data["A"]["claims"][0]
        data["A"]["claims"].extend([dict(source,id="200"),dict(source,id="201",decision="reject")])
        wire, lookup = build_source(data)
        self.assertEqual([o["id"] for o in wire["A"]["observations"]], ["A1","A2"])
        self.assertEqual(list(wire["A"]["quotes"]), ["Aq1"])
        self.assertNotIn("A1", lookup["B"])
        self.assertNotIn("raw_start", json.dumps(wire))
        cards = admit_cards({"A":{"common":slot("301","B1"),"spec.introduction":slot("302","A1")},
                             "B":{"common":slot("303","B1")}}, data, lookup)
        self.assertEqual(cards["A"]["common"], "")
        self.assertEqual(cards["A"]["spec"]["introduction"], "302")
        self.assertEqual(cards["A"]["issues"][0]["field"], "common")
        self.assertEqual(cards["B"]["common"], "303")

    def test_unknown_attributes_and_quote_selected_by_id(self):
        data = drafts()
        _, lookup = build_source(data)
        result = admit_cards({"A":{"character.appearance.height_cm":slot(175,"A1"),
                                   "character.age.fixed_age":slot(25,"A1"),
                                   "character.biography.occupation":slot("999","A1"),
                                   "character.examples":slot(["Aq1"],"A1")},
                              "B":{"character.examples":slot(["Aq1"],"B1")}}, data, lookup)
        a = result["A"]["character"]
        self.assertEqual(a["appearance"]["height_cm"], 175)
        self.assertIsNone(a["age"]["fixed_age"])
        self.assertIsNone(a["biography"]["occupation"])
        self.assertEqual(a["examples"][0]["text"], "175 cm")
        self.assertFalse(result["B"]["character"]["examples"])
        self.assertEqual(a, validate_character(a))

    def test_manual_edit_wins_and_conflicting_observations_are_visible(self):
        data = drafts()
        data["A"]["spec"]["expression_style"] = "700"
        data["A"]["edited_fields"] = ["expression_style"]
        data["A"]["claims"].append(dict(data["A"]["claims"][0],id="999",counter_evidence=data["A"]["claims"][0]["evidence"]))
        wire, lookup = build_source(data)
        self.assertEqual(wire["A"]["manual"], {"expression_style":"700"})
        result = admit_cards({"A":{"spec.expression_style":slot("701","A1"),"common":slot("702","A2"),
                                   "scenes.fallback":slot("703","A1")}}, data, lookup)
        self.assertEqual(result["A"]["spec"]["expression_style"], "700")
        self.assertFalse(result["A"]["common"])
        self.assertTrue(result["B"]["issues"])
        edited = deepcopy(result["A"])
        edited["scenes"]["fallback"] = "704"
        checked = validate_edit(edited, result["A"])
        self.assertNotIn("scenes.fallback", checked["sources"])
        self.assertEqual(checked["scenes"]["fallback"], "704")

    def test_nested_reply_separates_unreferenced_proposals_and_retains_matching_quotes(self):
        data = drafts()
        _, lookup = build_source(data)
        returned = {"A":{"spec":{"initial_background":"300"}, "scenes":{"daily":"501"},
                         "scenes_fallback":"502", "examples":["Aq1"]},
                    "B":{"common":{"value":"404","sources":["A1"]}, "examples":["Aq1"]}}
        cards = admit_cards(returned,data,lookup)
        self.assertEqual(cards["A"]["spec"]["initial_background"], "175")
        self.assertEqual(cards["A"]["character"]["examples"][0]["text"], "175 cm")
        self.assertEqual(cards["A"]["scenes"]["fallback"], "")
        proposals = {i["field"]:i["proposal"] for i in cards["A"]["issues"]}
        self.assertEqual(proposals, {"spec.initial_background":"300","scenes.daily":"501","scenes.fallback":"502"})
        self.assertFalse(cards["B"]["character"]["examples"])
        self.assertFalse(cards["B"]["common"])
        self.assertTrue(all("proposal" not in i for i in cards["B"]["issues"]))

    def workflow(self, value, data=None):
        from persona_runtime.import_card_fields import normalize_workbench_reply, workflow_cards
        data = data or drafts()
        _, refs = build_source(data)
        return workflow_cards(admit_cards(normalize_workbench_reply(value,refs), data, refs), data, value, refs)

    def test_workbench_prefills_role_prose_and_preserves_evidence_discipline(self):
        data = drafts()
        result = self.workflow({"A":{"common":"301", "scenes":{"daily":{"value":"302"}},
                                     "jev":{"instruction":{"value":"303","sources":[]}},
                                     "character":{"biography":{"occupation":"999"}},
                                     "spec":{"initial_background":"900"}},
                                "B":{"scenes":{"daily":"402"}, "common":slot("901","A1")}}, data)
        a, b = result["A"], result["B"]
        self.assertEqual([a["common"],a["scenes"]["daily"],a["jev"]["instruction"]], ["301","302","303"])
        self.assertEqual(set(a["generated_fields"]), {"common","scenes.daily","jev.instruction"})
        self.assertFalse(set(a["generated_fields"]) & a["sources"].keys())
        self.assertIsNone(a["character"]["biography"]["occupation"])
        self.assertEqual(a["spec"]["initial_background"],"175")
        self.assertEqual(b["common"],"")
        self.assertTrue(any(i.get("severity") == "conflict" and i["field"] == "common" for i in b["issues"]))
        self.assertTrue(all("proposal" not in i for i in b["issues"] if i["field"] == "common"))
        edited = deepcopy(a)
        edited["common"] = "304"
        self.assertNotIn("common",validate_edit(edited,a)["generated_fields"])

    def test_conflict_from_omitted_input_and_two_distinct_forms(self):
        from persona_runtime.import_card_fields import unresolved_conflicts
        data = drafts()
        data["A"]["claims"][0]["counter_evidence"] = deepcopy(data["A"]["claims"][0]["evidence"])
        value = {"A":{"scenes":{"help":"410"}}, "B":{"scenes":{"daily":"101","help":"702"},
                 "scenes.daily":"101","scenes.help":"703","common":"310"}}
        cards = self.workflow(value,data)
        original, = unresolved_conflicts(cards["A"])
        self.assertEqual(original["field"],"spec.initial_background")
        self.assertTrue(original["observations"][0]["counter_evidence"])
        self.assertEqual(cards["A"]["scenes"]["help"],"410")
        self.assertEqual(cards["B"]["common"],"310")
        self.assertEqual(cards["B"]["scenes"]["daily"],"101")
        self.assertEqual(cards["B"]["scenes"]["help"],"")
        self.assertEqual([i["field"] for i in unresolved_conflicts(cards["B"])], ["scenes.help"])
        self.assertEqual(unresolved_conflicts(cards["B"])[0]["alternatives"],
                         [{"field":"scenes.help","value":"702"},{"field":"scenes.help","value":"703"}])
        edited = deepcopy(cards["B"]); edited["issues"] = []
        self.assertTrue(unresolved_conflicts(validate_edit(edited,cards["B"])))
        with self.assertRaises(ValueError):
            validate_edit(edited,cards["B"],["scenes.daily"])
        confirmed = validate_edit(edited,cards["B"],["scenes.help"])
        self.assertFalse(unresolved_conflicts(confirmed))
        self.assertEqual(confirmed["scenes"]["help"],"")

    def test_incompatible_age_policies_allow_unrelated_voice(self):
        from persona_runtime.import_card_fields import unresolved_conflicts
        value = {"A":{"character.age.fixed_age":slot(20,"A1"),"character.age.birth_date":slot("2000-01-01","A1"),
                      "common":"100","scenes":{"closing":"200"}}, "B":{"common":"300"}}
        a = self.workflow(value)["A"]
        self.assertEqual({i["field"] for i in unresolved_conflicts(a)},
                         {"character.age.fixed_age","character.age.birth_date"})
        self.assertIsNone(a["character"]["age"]["fixed_age"])
        self.assertIsNone(a["character"]["age"]["birth_date"])
        self.assertEqual(a["scenes"]["closing"],"200")

    def test_conflicting_age_forms_retain_each_role_owned_quote_and_typed_value(self):
        from persona_runtime.import_card_fields import unresolved_conflicts
        data = drafts()
        c = data["A"]["claims"][0]
        c["evidence"] = [dict(c["evidence"][0],quote="25岁",date="2020-03-01",end=3)]
        data["A"]["claims"].append(dict(c,id="202",value="2000-03-01",
            evidence=[dict(c["evidence"][0],id="3",quote="2000-03-01",end=10)]))
        result = self.workflow({"A":{"character.age.fixed_age":slot(25,"A1"),
                                     "character.age.birth_date":slot("2000-03-01","A2")},
                                "B":{"common":slot("901","A2")}}, data)
        conflicts = unresolved_conflicts(result["A"])
        self.assertEqual(len(conflicts),2)
        for issue in conflicts:
            self.assertEqual(issue["alternatives"], [{"field":"character.age.fixed_age","value":25},
                                                      {"field":"character.age.birth_date","value":"2000-03-01"}])
            self.assertEqual([c["evidence"][0]["quote"] for c in issue["observations"]],["25岁","2000-03-01"])
            self.assertTrue(all(r["speaker"]=="A" for c in issue["observations"] for r in c["evidence"]))
        wrong, = unresolved_conflicts(result["B"])
        self.assertNotIn("alternatives",wrong)
        self.assertNotIn("observations",wrong)

    def test_consistent_dated_age_and_birth_select_native_calendar(self):
        from persona_runtime.import_card_fields import unresolved_conflicts
        data = drafts()
        c = data["A"]["claims"][0]
        c["evidence"] = [dict(c["evidence"][0],quote="20岁",date="2020-03-01",end=3)]
        data["A"]["claims"].append(dict(c,id="202",value="2000-03-01",
            evidence=[dict(c["evidence"][0],id="3",quote="2000-03-01",end=10)]))
        a = self.workflow({"A":{"character.age.fixed_age":slot(20,"A1"),
                                "character.age.birth_date":slot("2000-03-01","A2")},
                           "B":{"common":"310"}}, data)["A"]
        self.assertFalse(unresolved_conflicts(a))
        self.assertEqual(a["character"]["age"]["birth_date"],"2000-03-01")
        self.assertEqual(a["character"]["age"]["policy"],"calendar")
        self.assertIsNone(a["character"]["age"]["fixed_age"])

    def test_ordinary_preview_uses_current_native_instruction_and_core_order(self):
        from persona_runtime.companion_im import INSTRUCTION
        spec = drafts()["B"]["spec"]
        shown = ordinary_preview(spec)
        self.assertTrue(shown.startswith(INSTRUCTION))
        data = json.loads(shown[len(INSTRUCTION):])
        self.assertEqual(data,spec)
        self.assertEqual(list(data), ["format_version",*sorted({"name",*FIELDS}-{"expression_style"}),"expression_style"])
