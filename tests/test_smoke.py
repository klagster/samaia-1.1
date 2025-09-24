def test_imports():
    import app
    assert hasattr(app, "__package__")