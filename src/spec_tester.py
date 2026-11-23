import argparse
import base64
import datetime
import json
import struct
import sys
import urllib.error
import urllib.request
import uuid
import zlib
from functools import cached_property

STATE = "I was charged twice for my subscription this month. Please refund the extra charge."
PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAHElEQVR4nGP4TyJgoKcGBgbsaFQDfTUQB4aDBgDAen2fpitsVAAAAABJRU5ErkJggg=="
IMAGES = {"TypeSafe-Danger-Zone-Images": "true"}
LONG_CONTEXT = {"TypeSafe-Danger-Zone-Long-Context": "true"}
TOLERANCE = 0.02
DEFAULT_ADDRESS = "http://localhost:8000/v1/systemone/"

NOUL = {"type": "noul", "instructions": "Is this message about billing?"}
CHOICE = {
    "type": "choice",
    "instructions": "What is the tone of this message?",
    "criteria": {"angry": "An upset or hostile message", "calm": "A neutral or polite message", "excited": "An enthusiastic or eager message"},
}
SCORE = {"type": "score", "instructions": "How urgent is this message?", "criteria": ["Can wait", "Needs attention this week", "Needs attention today"]}
BOX = {"type": "box", "instructions": "Locate the red rectangle."}
ASSET = {"type": "image", "format": "png", "data_base64": PNG_BASE64}


class Failure(Exception):
    pass


def expect(condition, message):
    if not condition:
        raise Failure(message)


def is_number(value):
    return type(value) in (int, float)


def is_probability(value):
    return is_number(value) and 0 <= value <= 1


def decode(response):
    content = response.read()
    if response.headers.get_content_type() != "application/json":
        return f"non-JSON {response.headers.get_content_type()} body: {content.decode(errors='replace')}"
    try:
        return json.loads(content)
    except ValueError:
        return content.decode(errors="replace")


class Api:
    def __init__(self, address, lenient_models):
        self.address = address.rstrip("/").removesuffix("/v1/systemone")
        self.lenient_models = lenient_models

    def send(self, method, path, data=None, headers=None):
        request = urllib.request.Request(self.address + path, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                return response.status, decode(response)
        except urllib.error.HTTPError as error:
            return error.code, decode(error)

    def post(self, payload, headers=None):
        return self.post_raw(json.dumps(payload).encode(), "application/json", headers)

    def post_raw(self, data, content_type, headers=None):
        return self.send("POST", "/v1/systemone", data, {"Content-Type": content_type, **(headers or {})})

    def post_multipart(self, parts, headers=None):
        boundary = uuid.uuid4().hex
        chunks = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"{filename}\r\nContent-Type: {content_type}\r\n\r\n'.encode() + content + b"\r\n"
            for name, content_type, content, filename in parts
        ]
        return self.post_raw(b"".join(chunks) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}", headers)

    @cached_property
    def models(self):
        status, body = self.send("GET", "/v1/models")
        expect(status == 200 and isinstance(body, dict), f"GET /v1/models must return 200 with a JSON object, got {status}: {body}")
        if self.lenient_models and isinstance(body.get("data"), list):
            return [model.get("id") for model in body["data"] if isinstance(model, dict)]
        expect(isinstance(body.get("models"), list), f"GET /v1/models must return a models list, got {body}")
        return [model.get("name") for model in body["models"] if isinstance(model, dict)]

    @cached_property
    def model(self):
        expect(self.models, "GET /v1/models lists no model")
        return self.models[0]

    @cached_property
    def vision_model(self):
        for model in self.models:
            if self.post(request(model, {"box": BOX}, "photo", assets={"photo": ASSET}), IMAGES)[0] == 200:
                return model
        return None

    @cached_property
    def image_model(self):
        return self.vision_model or self.model


def request(model, questions, state=STATE, **extra):
    return {"model": model, "state": state, "questions": questions, **extra}


def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def rectangle_png(size, top, bottom, left, right):
    rows = b"".join(b"\0" + b"".join(b"\xff\0\0" if top <= y < bottom and left <= x < right else b"\xff\xff\xff" for x in range(size)) for y in range(size))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header) + png_chunk(b"IDAT", zlib.compress(rows)) + png_chunk(b"IEND", b"")


def json_part(payload):
    return ("request", "application/json", json.dumps(payload).encode(), "")


def image_part(name):
    return (name, "image/png", base64.b64decode(PNG_BASE64), f'; filename="{name}"')


def check_noul(name, question, answer):
    expect(is_probability(answer.get("noul")), f"{name}: noul must be a number from 0 to 1, got {answer.get('noul')!r}")


def check_choice(name, question, answer):
    probabilities = answer.get("probabilities")
    expect(isinstance(probabilities, dict) and probabilities.keys() == question["criteria"].keys(), f"{name}: probabilities must be keyed by the criteria, got {probabilities!r}")
    expect(all(is_probability(value) for value in probabilities.values()), f"{name}: every probability must be a number from 0 to 1")
    expect(abs(sum(probabilities.values()) - 1) <= TOLERANCE, f"{name}: probabilities must sum to about 1, got {sum(probabilities.values())}")
    expect(answer.get("choice") in probabilities, f"{name}: choice must be one of the criteria, got {answer.get('choice')!r}")
    expect(probabilities[answer["choice"]] == max(probabilities.values()), f"{name}: choice must have the highest probability")
    expect(is_probability(answer.get("confidence")), f"{name}: confidence must be a number from 0 to 1, got {answer.get('confidence')!r}")


def check_score(name, question, answer):
    levels = [str(level) for level in range(len(question["criteria"]))]
    expect(answer.get("legend") == dict(zip(levels, question["criteria"])), f"{name}: legend must map each level to its criterion, got {answer.get('legend')!r}")
    probabilities = answer.get("probabilities")
    expect(isinstance(probabilities, dict) and sorted(probabilities) == sorted(levels), f"{name}: probabilities must use the legend keys, got {probabilities!r}")
    expect(all(is_probability(value) for value in probabilities.values()), f"{name}: every probability must be a number from 0 to 1")
    total = sum(probabilities.values())
    expect(abs(total - 1) <= TOLERANCE, f"{name}: probabilities must sum to about 1, got {total}")
    mean = sum(int(level) * value for level, value in probabilities.items()) / total
    expect(is_number(answer.get("score")) and abs(answer["score"] - mean) <= TOLERANCE, f"{name}: score must be the probability-weighted level {mean}, got {answer.get('score')!r}")
    expect(is_probability(answer.get("confidence")), f"{name}: confidence must be a number from 0 to 1, got {answer.get('confidence')!r}")


def check_box(name, question, answer):
    box = answer.get("box")
    expect(isinstance(box, dict) and all(is_probability(box.get(edge)) for edge in ("top", "bottom", "left", "right")), f"{name}: box edges must be numbers from 0 to 1, got {box!r}")
    expect("confidence" in answer and (answer["confidence"] is None or is_probability(answer["confidence"])), f"{name}: confidence must be null or a number from 0 to 1")


CHECKS = {"noul": check_noul, "choice": check_choice, "score": check_score, "box": check_box}


def answered(questions, response):
    status, body = response
    expect(status == 200, f"expected 200, got {status}: {body}")
    expect(isinstance(body, dict), f"response must be a JSON object, got {body!r}")
    expect(isinstance(body.get("model"), str), f"model must be a string, got {body.get('model')!r}")
    usage = body.get("usage")
    expect(isinstance(usage, dict) and all(type(usage.get(field)) is int and usage[field] >= 0 for field in ("input_tokens", "output_tokens")), f"usage must hold non-negative integer token counts, got {usage!r}")
    answers = body.get("answers")
    expect(isinstance(answers, dict) and answers.keys() == questions.keys(), f"answers must be keyed by the question names {list(questions)}, got {answers!r}")
    for name, question in questions.items():
        answer = answers[name]
        expect(isinstance(answer, dict) and answer.get("type") == question["type"], f"{name}: answer type must be {question['type']!r}, got {answer!r}")
        CHECKS[question["type"]](name, question, answer)
    return body


def refused(response):
    status, body = response
    expect(status in (400, 422), f"expected 400 or 422, got {status}: {body}")
    expect(isinstance(body, dict), f"error body must be a JSON object, got {body!r}")
    if status == 400:
        expect(isinstance(body.get("detail"), (str, dict)), f"400 detail must be a string or an object, got {body!r}")
        return
    expect(isinstance(body.get("detail", []), list), f"422 detail must be a list, got {body!r}")
    for error in body.get("detail", []):
        expect(isinstance(error, dict) and isinstance(error.get("loc"), list) and all(type(part) in (str, int) for part in error["loc"]), f"422 loc must be a list of strings and integers, got {error!r}")
        expect(isinstance(error.get("msg"), str) and isinstance(error.get("type"), str), f"422 errors need a string msg and type, got {error!r}")
        expect(isinstance(error.get("input", ""), str) and isinstance(error.get("ctx", {}), dict), f"422 input must be a string and ctx an object, got {error!r}")


def accepts(api, questions, state=STATE, model=None, headers=None, **extra):
    return answered(questions, api.post(request(model or api.model, questions, state, **extra), headers))


def rejects(api, questions, state=STATE, model=None, headers=None, **extra):
    refused(api.post(request(model or api.model, questions, state, **extra), headers))


def fits(api, questions, state=STATE, **extra):
    response = api.post(request(api.model, questions, state, **extra))
    if response[0] == 200:
        return answered(questions, response)
    return refused(response)


def seen(api, questions, response):
    if api.vision_model:
        return answered(questions, response)
    return refused(response)


def sees(api, questions, assets, state="photo", headers=IMAGES):
    return seen(api, questions, api.post(request(api.image_model, questions, state, assets=assets), headers))


def blind(api, questions, assets, state="photo", headers=IMAGES):
    rejects(api, questions, state, api.image_model, headers, assets=assets)


def test_models_endpoint(api):
    if api.lenient_models:
        for model in api.models:
            expect(isinstance(model, str), f"every listed model needs a string name, got {model!r}")
            accepts(api, {"billing": NOUL}, model=model)
        return
    status, body = api.send("GET", "/v1/models")
    expect(status == 200, f"expected 200, got {status}: {body}")
    expect(isinstance(body, dict) and isinstance(body.get("models"), list), f"models must be a list, got {body!r}")
    for model in body["models"]:
        expect(isinstance(model, dict) and all(isinstance(model.get(field), str) for field in ("name", "description", "release_date")), f"model needs string name, description and release_date, got {model!r}")
        expect(len(model["release_date"]) == 10 and datetime.date.fromisoformat(model["release_date"]), f"release_date must be YYYY-MM-DD, got {model['release_date']!r}")
        accepts(api, {"billing": NOUL}, model=model["name"])


def test_model_field(api):
    accepts(api, {"billing": NOUL})
    payload = request(api.model, {"billing": NOUL})
    del payload["model"]
    refused(api.post(payload))
    for model in (42, None, True, ["m"], {"name": api.model}, "", f"no-such-model-{uuid.uuid4().hex}"):
        refused(api.post({**payload, "model": model}))


def test_state_field(api):
    nested = {"level": 0}
    for depth in range(1, 20):
        nested = {"level": depth, "inner": nested}
    for state in (STATE, {"subject": "Duplicate charge", "message": "Please help."}, [{"from": "customer", "text": "Refund please"}], "", "   ", {}, [], "Facturé deux fois 💳 二重請求", [[1, 2], [3, [4, None, True]]]):
        accepts(api, {"billing": NOUL}, state)
    for state in (nested, " ".join([STATE] * 20)):
        fits(api, {"billing": NOUL}, state)
    payload = request(api.model, {"billing": NOUL})
    del payload["state"]
    refused(api.post(payload))
    for state in (42, 1.5, True, False, None):
        rejects(api, {"billing": NOUL}, state)


def test_questions_field(api):
    accepts(api, {"billing": NOUL})
    accepts(api, {"with space": NOUL, "ünïcödé-名前": NOUL, "0": NOUL, "": NOUL, "a.b/c": NOUL})
    accepts(api, {f"question {index}": NOUL for index in range(32)})
    payload = request(api.model, {"billing": NOUL})
    del payload["questions"]
    refused(api.post(payload))
    for questions in ({}, [], [NOUL], "billing", None, 42, {"billing": "Is it?"}, {"billing": None}, {"billing": [NOUL]}):
        rejects(api, questions)


def test_question_type(api):
    accepts(api, {"billing": NOUL, "tone": CHOICE, "urgency": SCORE})
    for kind in (None, "bool", "Noul", "NOUL", " noul", "", 1, True, ["noul"]):
        rejects(api, {"q": {"type": kind, "instructions": "Is it?"}})
    rejects(api, {"q": {"instructions": "Is it?"}})


def test_instructions_field(api):
    forms = ("Is this about billing?", {"task": "Is this about billing?"}, ["Is this", "about billing?"], "", "   ", "Est-ce une facture ? 請求ですか")
    questions = {f"noul {index}": {"type": "noul", "instructions": form} for index, form in enumerate(forms)}
    questions |= {f"choice {index}": {**CHOICE, "instructions": form} for index, form in enumerate(forms)}
    questions |= {f"score {index}": {**SCORE, "instructions": form} for index, form in enumerate(forms)}
    accepts(api, questions)
    accepts(api, {"noul": {"type": "noul", "instructions": None, "criteria": {"true": "About billing"}}, "choice": {"type": "choice", "criteria": CHOICE["criteria"]}, "score": {"type": "score", "instructions": None, "criteria": SCORE["criteria"]}})
    for instructions in (42, 1.5, True, False):
        for question in (NOUL, CHOICE, SCORE):
            rejects(api, {"q": {**question, "instructions": instructions}})


def test_unknown_fields(api):
    accepts(api, {"billing": {**NOUL, "note": "unread"}, "tone": {**CHOICE, "weight": 2}, "urgency": {**SCORE, "extra": [1]}}, temperature=0.5, seed=1)
    accepts(api, {"spam": {"type": "noul", "instructions": "Is this spam?", "criteria": {"true": "Advertising", "maybe": "unread"}}})


def test_noul_question(api):
    accepts(api, {"billing": NOUL})
    accepts(api, {"statement": {"type": "noul", "instructions": "This message is about billing."}})


def test_noul_criteria(api):
    accepts(api, {
        "both": {"type": "noul", "instructions": "Is this spam?", "criteria": {"true": "Unsolicited advertising", "false": "A legitimate conversation"}},
        "structured": {"type": "noul", "instructions": "Is it urgent?", "criteria": {"true": {"level": "high"}, "false": ["low", "none"]}},
        "true_only": {"type": "noul", "criteria": {"true": "The message asks for a refund."}},
        "false_only": {"type": "noul", "criteria": {"false": "The message is not about money."}},
        "null_criteria": {"type": "noul", "instructions": "This message is about billing.", "criteria": None},
        "null_outcomes": {"type": "noul", "instructions": "Is it polite?", "criteria": {"true": None, "false": None}},
        "empty_criteria": {"type": "noul", "instructions": "Is it polite?", "criteria": {}},
        "empty_outcomes": {"type": "noul", "instructions": "Is it polite?", "criteria": {"true": "", "false": ""}},
    })
    for criteria in ("Unsolicited advertising", ["yes", "no"], 42, True, {"true": 1}, {"false": True}, {"true": 1.5}):
        rejects(api, {"q": {"type": "noul", "instructions": "Is it?", "criteria": criteria}})


def test_choice_question(api):
    accepts(api, {"tone": CHOICE})
    accepts(api, {"tone": {**CHOICE, "criteria": {"angry": None, "calm": None}}})


def test_choice_criteria(api):
    accepts(api, {"intent": {"type": "choice", "instructions": "What does the customer want?", "criteria": {"refund": "Asks for money back", "bare": None, "structured": {"means": "a cancellation"}, "listed": ["a question", "about pricing"], "empty": ""}}})
    accepts(api, {"names": {"type": "choice", "instructions": "Pick one.", "criteria": {"": None, " ": None, "A": None, "a": None, "名前": None, "None of the above": None, "with space": None, "0": None}}})
    for criteria in (None, ["angry", "calm"], "angry", 42, {"angry": 42}, {"angry": True}, {"angry": 1.5}):
        rejects(api, {"q": {"type": "choice", "instructions": "Which tone?", "criteria": criteria}})
    rejects(api, {"q": {"type": "choice", "instructions": "Which tone?"}})


def test_choice_bounds(api):
    for count in (1, 2, 255):
        fits(api, {"pick": {"type": "choice", "instructions": "Pick the best option.", "criteria": {f"option {index}": None for index in range(count)}}})
    for count in (0, 256, 1000):
        rejects(api, {"pick": {"type": "choice", "criteria": {f"option {index}": None for index in range(count)}}})


def test_score_question(api):
    accepts(api, {"urgency": SCORE})
    accepts(api, {"urgency": {**SCORE, "criteria": ["low", "high"]}})


def test_score_criteria(api):
    accepts(api, {"severity": {"type": "score", "instructions": "How severe is it?", "criteria": [{"what": "cosmetic", "examples": ["typo"]}, ["broken", "feature"], "outage"]}})
    accepts(api, {"edges": {"type": "score", "instructions": "Rate it.", "criteria": ["same", "same", "", "   ", "名前", {}, []]}})
    for criteria in (None, {"0": "low"}, "low", 42, ["low", None], ["low", 1], ["low", True], ["low", 1.5]):
        rejects(api, {"q": {"type": "score", "instructions": "How urgent?", "criteria": criteria}})
    rejects(api, {"q": {"type": "score", "instructions": "How urgent?"}})


def test_score_bounds(api):
    for count in (1, 2, 10):
        accepts(api, {"rating": {"type": "score", "instructions": "Rate it.", "criteria": [f"level {index}" for index in range(count)]}})
    for count in (0, 11, 100):
        rejects(api, {"rating": {"type": "score", "criteria": [f"level {index}" for index in range(count)]}})


def test_box_question(api):
    sees(api, {"box": BOX}, {"photo": ASSET})
    sees(api, {"bare": {"type": "box"}, "null": {"type": "box", "instructions": None}, "object": {"type": "box", "instructions": {"find": "the red rectangle"}}, "array": {"type": "box", "instructions": ["Locate", "the red rectangle."]}}, {"photo": ASSET})
    sees(api, {"box": BOX, "red": {"type": "noul", "instructions": "Is there something red in photo?"}}, {"photo": ASSET})
    rejects(api, {"box": BOX}, "photo", api.image_model, IMAGES)
    for instructions in (42, True):
        blind(api, {"box": {"type": "box", "instructions": instructions}}, {"photo": ASSET})


def test_noul_semantics(api):
    answers = accepts(api, {"billing": NOUL, "weather": {"type": "noul", "instructions": "Is this message about the weather?"}})["answers"]
    expect(answers["billing"]["noul"] > 0.5, f"a billing message must favor yes on billing, got {answers['billing']['noul']}")
    expect(answers["weather"]["noul"] < 0.5, f"a billing message must favor no on weather, got {answers['weather']['noul']}")


def test_choice_semantics(api):
    for state, tone in (("I HATE this company! This is the worst service ever and I am absolutely furious!", "angry"), ("Hello, could you please send me last month's invoice? Thank you.", "calm")):
        answer = accepts(api, {"tone": CHOICE}, state)["answers"]["tone"]
        expect(answer["choice"] == tone, f"{state!r} must be chosen as {tone}, got {answer['choice']!r}")


def test_score_semantics(api):
    calm = "No rush at all, whenever you have time: there is a small typo on our about page."
    urgent = "URGENT: production is down, every customer is affected and we are losing money every minute."
    scores = [accepts(api, {"urgency": SCORE}, state)["answers"]["urgency"]["score"] for state in (calm, urgent)]
    expect(scores[0] < scores[1], f"an outage must score as more urgent than a typo, got {scores}")


def test_box_semantics(api):
    asset = {"type": "image", "format": "png", "data_base64": base64.b64encode(rectangle_png(64, 8, 24, 32, 56)).decode()}
    body = sees(api, {"box": BOX}, {"photo": asset})
    if body is None:
        return
    box = body["answers"]["box"]["box"]
    expect(box["top"] < box["bottom"] and box["left"] < box["right"], f"box edges must enclose an area, got {box}")
    expect(0.125 <= (box["top"] + box["bottom"]) / 2 <= 0.375 and 0.5 <= (box["left"] + box["right"]) / 2 <= 0.875, f"box must be centered on the red rectangle at top 0.125, bottom 0.375, left 0.5, right 0.875, got {box}")


def test_usage(api):
    usage = accepts(api, {"billing": NOUL})["usage"]
    expect(usage["input_tokens"] > 0, f"input_tokens must count the evaluated input, got {usage['input_tokens']}")
    more = accepts(api, {"billing": NOUL}, " ".join([STATE] * 3))["usage"]
    expect(more["input_tokens"] > usage["input_tokens"], f"a longer state must use more input tokens, got {more['input_tokens']} after {usage['input_tokens']}")


def test_request_body(api):
    answered({"billing": NOUL}, api.post_raw(json.dumps(request(api.model, {"billing": NOUL})).encode(), "application/json; charset=utf-8"))
    for data in (b"", b'{"model": ', b"[]", b"null", b'"text"', b"42", b"\xff\xfe"):
        refused(api.post_raw(data, "application/json"))
    for content_type in ("text/plain", "application/x-www-form-urlencoded"):
        refused(api.post_raw(json.dumps(request(api.model, {"billing": NOUL})).encode(), content_type))


def test_long_context_header(api):
    accepts(api, {"billing": NOUL}, headers=LONG_CONTEXT)
    accepts(api, {"billing": NOUL}, headers={"typesafe-danger-zone-long-context": "true"})
    for value in ("false", "TRUE", "1", "yes", ""):
        rejects(api, {"billing": NOUL}, headers={"TypeSafe-Danger-Zone-Long-Context": value})


def test_images_header(api):
    sees(api, {"box": BOX}, {"photo": ASSET}, headers={"typesafe-danger-zone-images": "true"})
    accepts(api, {"billing": NOUL}, headers=IMAGES)
    blind(api, {"box": BOX}, {"photo": ASSET}, headers=None)
    blind(api, {"billing": NOUL}, {"photo": ASSET}, headers=None)
    rejects(api, {"box": BOX}, "photo", api.image_model)
    for value in ("false", "TRUE", "1", "yes", ""):
        blind(api, {"box": BOX}, {"photo": ASSET}, headers={"TypeSafe-Danger-Zone-Images": value})


def test_assets(api):
    sees(api, {"box": BOX}, {"photo": ASSET})
    sees(api, {"first": {"type": "box", "instructions": "Locate the red rectangle in first."}, "second": {"type": "box", "instructions": "Locate the red rectangle in second."}}, {"first": ASSET, "second": ASSET}, "first second")
    sees(api, {"box": BOX}, {"photo": {"format": "png", "data_base64": PNG_BASE64}})
    sees(api, {"red": {"type": "noul", "instructions": "Is there something red in photo?"}}, {"photo": ASSET})
    for declared in ("png", "jpg", "jpeg"):
        sees(api, {"box": BOX}, {"photo": {**ASSET, "format": declared}})


def test_asset_validation(api):
    for assets in (
        {},
        [ASSET],
        "photo",
        {"photo": None},
        {"photo": PNG_BASE64},
        {"photo": {"type": "image", "data_base64": PNG_BASE64}},
        {"photo": {"type": "image", "format": "png"}},
        {"photo": {**ASSET, "format": "gif"}},
        {"photo": {**ASSET, "format": "PNG"}},
        {"photo": {**ASSET, "type": "video"}},
        {"photo": {**ASSET, "data_base64": ""}},
        {"photo": {**ASSET, "data_base64": "bm90IGFuIGltYWdl"}},
        {"photo": {**ASSET, "data_base64": "%%%"}},
        {"photo": {**ASSET, "data_base64": 42}},
        {"photo": {**ASSET, "part": "image-0"}},
    ):
        blind(api, {"box": BOX}, assets)


def test_asset_placeholders(api):
    placeholder = str(uuid.uuid4())
    questions = {
        "box": {"type": "box", "instructions": f"Locate the red rectangle in {placeholder}."},
        "red": {"type": "noul", "instructions": f"Does {placeholder} contain red?", "criteria": {"true": f"{placeholder} shows red"}},
        "shape": {"type": "choice", "instructions": "What is shown?", "criteria": {"rectangle": f"A rectangle as in {placeholder}", "nothing": "No shape"}},
        "size": {"type": "score", "instructions": f"How large is the shape in {placeholder}?", "criteria": ["small", "large"]},
    }
    sees(api, questions, {placeholder: ASSET}, {"image": placeholder, "caption": f"see {placeholder}", "nested": [placeholder]})
    sees(api, {"box": BOX}, {placeholder: ASSET}, placeholder)


def test_multipart(api):
    answered({"billing": NOUL}, api.post_multipart([json_part(request(api.model, {"billing": NOUL}))]))
    answered({"tone": CHOICE}, api.post_multipart([("request", "application/json; charset=utf-8", json.dumps(request(api.model, {"tone": CHOICE})).encode(), "")]))


def test_multipart_images(api):
    payload = request(api.image_model, {"box": BOX}, "photo", assets={"photo": {"type": "image", "part": "image-0"}})
    seen(api, {"box": BOX}, api.post_multipart([json_part(payload), image_part("image-0")], IMAGES))
    overridden = request(api.image_model, {"box": BOX}, "photo", assets={"photo": {"type": "image", "part": "image-0", "format": "png", "data_base64": "%%%"}})
    seen(api, {"box": BOX}, api.post_multipart([json_part(overridden), image_part("image-0")], IMAGES))
    questions = {"first": {"type": "box", "instructions": "Locate the red rectangle in first."}, "second": {"type": "box", "instructions": "Locate the red rectangle in second."}}
    mixed = request(api.image_model, questions, "first second", assets={"first": {"type": "image", "part": "image-0"}, "second": ASSET})
    seen(api, questions, api.post_multipart([json_part(mixed), image_part("image-0")], IMAGES))
    both = request(api.image_model, questions, "first second", assets={"first": {"type": "image", "part": "image-0"}, "second": {"type": "image", "part": "image-1"}})
    seen(api, questions, api.post_multipart([json_part(both), image_part("image-0"), image_part("image-1")], IMAGES))


def test_multipart_validation(api):
    payload = request(api.image_model, {"box": BOX}, "photo", assets={"photo": {"type": "image", "part": "image-0"}})
    shared = request(api.image_model, {"box": BOX}, "photo second", assets={"photo": {"type": "image", "part": "image-0"}, "second": {"type": "image", "part": "image-0"}})
    for parts, headers in (
        ([image_part("image-0")], IMAGES),
        ([image_part("image-0"), json_part(payload)], IMAGES),
        ([json_part(payload)], IMAGES),
        ([json_part(payload), image_part("image-0"), image_part("image-1")], IMAGES),
        ([json_part(shared), image_part("image-0")], IMAGES),
        ([json_part(payload), image_part("image-0"), image_part("image-0")], IMAGES),
        ([json_part({**payload, "assets": {"photo": {"type": "image", "part": ""}}}), image_part("")], IMAGES),
        ([json_part(payload), image_part("image-0")], None),
        ([("request", "application/json", b'{"model": ', "")], None),
        ([json_part(request(api.model, {"billing": NOUL})), image_part("image-0")], IMAGES),
    ):
        refused(api.post_multipart(parts, headers))


def paint(color, text):
    if not sys.stdout.isatty():
        return text
    return f"\033[{color}m{text}\033[0m"


TESTS = [value for name, value in list(globals().items()) if name.startswith("test_")]


def main() -> None:
    parser = argparse.ArgumentParser(prog="spec-tester")
    parser.add_argument("address", nargs="?", default=DEFAULT_ADDRESS)
    parser.add_argument("--strict-models", action="store_true", help="require the spec's /v1/models shape with full model metadata")
    arguments = parser.parse_args()
    api = Api(arguments.address, not arguments.strict_models)
    try:
        if api.vision_model is None:
            print(paint("33", "NOTE"), "no listed model answers box questions: image tests check that image requests are refused")
    except Exception as error:
        print(paint("33", "NOTE"), f"could not look for a vision model: {error}")
    failed = 0
    for test in TESTS:
        try:
            test(api)
            print(paint("32", "PASS"), test.__name__)
        except Exception as error:
            failed += 1
            print(paint("31", "FAIL"), paint("1", f"{test.__name__}:"), paint("2", f"{type(error).__name__}: {error}"))
    print(paint("1;31" if failed else "1;32", f"{len(TESTS) - failed}/{len(TESTS)} passed"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
