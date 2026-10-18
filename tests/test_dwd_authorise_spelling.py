"""Live on mdharan.com: a British-English Admin console labels the button "Authorise";
looking only for "Authorize" found nothing and the delegation grant timed out."""
import dwd_helper


def test_both_spellings_find_the_button():
    for label in ("Authorize", "Authorise", "AUTHORIZE", "AUTHORISE"):
        assert dwd_helper.AUTHORIZE.search(label)
    assert not dwd_helper.AUTHORIZE.search("Cancel")
