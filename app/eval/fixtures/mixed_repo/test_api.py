
from api import get_greeting

def test_greeting():
    assert get_greeting("World") == "Hello, World!"
