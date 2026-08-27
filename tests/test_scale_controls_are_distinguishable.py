"""Two dropdowns labelled "Scale", both visible, feeding different requests.

The Seed Wizard's Manual tab renders the Google sign-in card AND the
standalone seeding form. Each had a scale selector labelled exactly
"Scale", so setting one and pressing "Start seeding" in the other sent the
untouched default. Live, a run selected as large reached the seeder as:

    Seeding 201 users in source.rohitrokaya.com.np at scale 'small'
      ~60 messages and ~20 events per user

a tenth of the intended volume, with nothing on screen disagreeing --
because the control that was changed and the control that was read were
different controls with the same name.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    return open(os.path.join(ROOT, "migration-webui", "src", rel),
                encoding="utf-8").read()


class TestTheLabelsDiffer:
    def test_the_sign_in_cards_scale_is_named_for_its_own_seed(self):
        src = _read("components/QuickTenantSetup.tsx")
        assert 'label="Seed scale"' in src
        assert 'label="Scale"' not in src, (
            "the sign-in card is back to the ambiguous name")

    def test_the_seeding_forms_scale_keeps_the_plain_name(self):
        assert 'label="Scale"' in _read("pages/SeedWizard.tsx")

    def test_the_seeding_forms_control_is_addressable(self):
        # So a driver never has to guess by position again.
        assert "'data-testid': 'seed-form-scale'" in _read("pages/SeedWizard.tsx")

    def test_no_two_scale_labels_are_identical_across_the_pair(self):
        labels = []
        for rel in ("components/QuickTenantSetup.tsx", "pages/SeedWizard.tsx"):
            labels += re.findall(r'label="([^"]*[Ss]cale[^"]*)"', _read(rel))
        assert labels, "no scale controls found at all -- did they move?"
        assert len(set(labels)) == len(set(l for l in labels)), labels
        # The real property: the seeding form's name is not shared.
        assert labels.count("Scale") == 1, (
            f"more than one control is named exactly 'Scale': {labels}")
