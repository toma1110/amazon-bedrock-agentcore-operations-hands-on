import unittest

from converse_steps import call_model_once, read_response, run_tool_cycle


class FakeConverse:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def converse(self, **request):
        self.requests.append(request)
        return self.responses.pop(0)


def text_response(text="model reply"):
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
    }


def tool_response(name="lookup_runbook", input_value=None):
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "tooluse-1",
                            "name": name,
                            "input": input_value or {"service": "catalog-api", "topic": "5xx"},
                        }
                    }
                ],
            }
        },
        "stopReason": "tool_use",
    }


def tool_response_with_id(tool_use_id, input_value=None):
    response = tool_response(input_value=input_value)
    response["output"]["message"]["content"][0]["toolUse"]["toolUseId"] = tool_use_id
    return response


class ConverseStepsTests(unittest.TestCase):
    def test_response_reader_extracts_text_stop_reason_and_usage(self):
        response = text_response("answer")
        response["usage"] = {"inputTokens": 4, "outputTokens": 2}

        self.assertEqual(
            read_response(response),
            {
                "role": "assistant",
                "text": "answer",
                "stop_reason": "end_turn",
                "usage": {"inputTokens": 4, "outputTokens": 2},
            },
        )

    def test_single_call_sends_only_user_text_and_no_tool_config(self):
        client = FakeConverse([text_response("hi")])

        response = call_model_once(client, "model-id", "hello")

        self.assertEqual(response["stopReason"], "end_turn")
        self.assertEqual(client.requests[0]["messages"], [
            {"role": "user", "content": [{"text": "hello"}]}
        ])
        self.assertNotIn("toolConfig", client.requests[0])

    def test_tool_use_is_executed_by_app_then_returned_to_model(self):
        client = FakeConverse([tool_response(), text_response("summary")])
        observed = []

        result = run_tool_cycle(
            client,
            "model-id",
            "catalog-api 5xx",
            lambda service, topic: observed.append((service, topic)) or {"found": True},
        )

        self.assertEqual(observed, [("catalog-api", "5xx")])
        self.assertEqual(result["tool_calls"][0]["result"], {"found": True})
        second_messages = client.requests[1]["messages"]
        self.assertEqual(second_messages[-1]["content"][0]["toolResult"]["toolUseId"], "tooluse-1")
        self.assertEqual(second_messages[-1]["content"][0]["toolResult"]["content"], [{"json": {"found": True}}])

    def test_no_tool_request_does_not_execute_local_function(self):
        client = FakeConverse([text_response("Need more detail")])
        invoked = []

        result = run_tool_cycle(client, "model-id", "unclear", lambda *_: invoked.append(True))

        self.assertEqual(invoked, [])
        self.assertEqual(result["tool_calls"], [])
        self.assertEqual(len(client.requests), 1)

    def test_unknown_tool_is_rejected_before_execution(self):
        client = FakeConverse([tool_response(name="run_shell")])

        with self.assertRaisesRegex(ValueError, "Unsupported tool"):
            run_tool_cycle(client, "model-id", "question", lambda *_: {})
        self.assertEqual(len(client.requests), 1)

    def test_non_object_tool_input_is_rejected_before_callback(self):
        client = FakeConverse([tool_response(input_value=["catalog-api", "5xx"])])
        invoked = []

        with self.assertRaisesRegex(ValueError, "input must be an object"):
            run_tool_cycle(client, "model-id", "question", lambda *_: invoked.append(True))
        self.assertEqual(invoked, [])

    def test_invalid_tool_use_id_is_rejected_before_callback(self):
        for tool_use_id in (None, 7, "", "x" * 65):
            with self.subTest(tool_use_id=tool_use_id):
                client = FakeConverse([tool_response_with_id(tool_use_id)])
                invoked = []
                with self.assertRaisesRegex(ValueError, "toolUseId"):
                    run_tool_cycle(client, "model-id", "question", lambda *_: invoked.append(True))
                self.assertEqual(invoked, [])

    def test_non_text_response_block_is_not_silently_ignored(self):
        response = text_response()
        response["output"]["message"]["content"] = [{"image": {"format": "png", "source": {}}}]

        with self.assertRaisesRegex(ValueError, "unsupported content blocks"):
            read_response(response)

    def test_second_tool_request_is_rejected_as_unfinished(self):
        client = FakeConverse([tool_response(), tool_response()])
        invoked = []

        with self.assertRaisesRegex(ValueError, "second tool request"):
            run_tool_cycle(client, "model-id", "question", lambda *args: invoked.append(args) or {})
        self.assertEqual(invoked, [("catalog-api", "5xx")])
        self.assertEqual(len(client.requests), 2)


if __name__ == "__main__":
    unittest.main()
