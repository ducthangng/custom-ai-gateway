"""JJ, the router: it reads routes.yaml and decides which route a prompt belongs to."""

from gateway.routing import RuleClassifier, load_routes

router = RuleClassifier(load_routes("routes.yaml"))

def test_coding_question_goes_to_code_route():
    assert router.classify("why does my go program deadlock").route == "code"


def test_joke_goes_to_chitchat_route():
    assert router.classify("hey, tell me a joke").route == "chitchat"
