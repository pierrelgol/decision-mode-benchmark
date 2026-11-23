from bench import base_url, model_from

assert base_url("http://h:1/v1/systemone/") == "http://h:1"
assert base_url("http://h:1/") == "http://h:1"
assert model_from({"data": [{"id": "a"}]}) == "a"
assert model_from({"models": [{"name": "b"}]}) == "b"
for payload in ({"data": []}, {"data": [{"id": "a"}, {"id": "b"}]}):
    try:
        model_from(payload)
    except SystemExit:
        continue
    raise AssertionError(payload)
print("ok")
