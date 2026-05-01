#

# a simple wrapper for LLM calling

import time
import requests
from .utils import wrapped_trying, rprint, GET_ENV_VAR, KwargsInitializable

from transformers import AutoTokenizer

class MessageTruncator:
    def __init__(self, model_name="Qwen/Qwen3-32B"):
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)

    def _count_text_tokens(self, content):
        """
        Count tokens in a message's content.
        Handles both string and list-of-dict (multimodal) content.
        """
        if isinstance(content, str):
            return len(self.tokenizer.encode(content, add_special_tokens=False))
        elif isinstance(content, list):
            total = 0
            for part in content:
                if part.get("type") == "text":
                    total += len(self.tokenizer.encode(part.get("text", ""), add_special_tokens=False))
            return total
        else:
            return 0

    def _truncate_text_content(self, content, max_tokens):
        """
        Truncate text in content to fit max_tokens.
        For multimodal, only truncate the last text part if needed.
        """
        if isinstance(content, str):
            tokens = self.tokenizer.encode(content, add_special_tokens=False)
            truncated_tokens = tokens[:max_tokens]
            return self.tokenizer.decode(truncated_tokens)
        elif isinstance(content, list):
            # Go through parts, keep images, truncate last text if needed
            new_content = []
            tokens_used = 0
            for i, part in enumerate(content):
                if part.get("type") == "text":
                    text = part.get("text", "")
                    tokens = self.tokenizer.encode(text, add_special_tokens=False)
                    if tokens_used + len(tokens) > max_tokens:
                        # Truncate this text part
                        remaining = max_tokens - tokens_used
                        truncated_tokens = tokens[:remaining]
                        truncated_text = self.tokenizer.decode(truncated_tokens)
                        if truncated_text:
                            new_content.append({"type": "text", "text": truncated_text})
                        break  # No more tokens allowed after this
                    else:
                        new_content.append(part)
                        tokens_used += len(tokens)
                else:
                    # Always keep images
                    new_content.append(part)
            return new_content
        else:
            return content

    def truncate_message_list(self, messages, max_length):
        """
        Truncate a list of messages so that the total token count does not exceed max_length.
        Keeps the most recent messages. If the most recent message alone exceeds max_length,
        its text content will be truncated to fit. Images are never truncated or removed.
        """
        truncated = []
        total_tokens = 0
        for msg in reversed(messages):
            content = msg.get("content", "")
            tokens = self._count_text_tokens(content)
            if total_tokens + tokens > max_length:
                if not truncated:
                    # Truncate the most recent message's text content to fit max_length
                    truncated_content = self._truncate_text_content(content, max_length)
                    truncated_msg = msg.copy()
                    truncated_msg["content"] = truncated_content
                    truncated.insert(0, truncated_msg)
                break
            truncated.insert(0, msg)
            total_tokens += tokens
        return truncated

# --
# helper
def update_stat(stat, call_return):
    usage = call_return.get("usage", {})
    if stat is not None:
        stat["llm_call"] = stat.get("llm_call", 0) + 1
        for k in ['completion_tokens', 'prompt_tokens', 'total_tokens']:
            k = {'outputTokens': 'completion_tokens', 'inputTokens': 'prompt_tokens', 'totalTokens': 'total_tokens'}.get(k, k) # handling keys in claude
            stat[k] = stat.get(k, 0) + usage.get(k, 0)
# --

class OpenaiHelper:
    _openai_clients = {}  # model_name -> Helper

    @staticmethod
    def get_openai_client(model_name="", api_endpoint="", api_key=""):
        model_name_suffix = f"_{model_name}" if model_name else ""
        if model_name not in OpenaiHelper._openai_clients:  # lazy init
            import openai
            if GET_ENV_VAR("AZURE_OPENAI_API_KEY", f"AZURE_OPENAI_API_KEY{model_name_suffix}"):
                client = openai.AzureOpenAI(
                    azure_endpoint=GET_ENV_VAR("AZURE_OPENAI_ENDPOINT", f"AZURE_OPENAI_ENDPOINT{model_name_suffix}", df=api_endpoint),
                    api_key=GET_ENV_VAR("AZURE_OPENAI_API_KEY", f"AZURE_OPENAI_API_KEY{model_name_suffix}", df=api_key),
                    api_version=GET_ENV_VAR("AZURE_OPENAI_API_VERSION", df="2024-02-01")
                )
            else:
                client = openai.OpenAI(
                    base_url=GET_ENV_VAR("OPENAI_ENDPOINT", f"OPENAI_ENDPOINT{model_name_suffix}", df=api_endpoint),
                    api_key=GET_ENV_VAR("OPENAI_API_KEY", f"OPENAI_API_KEY{model_name_suffix}", df=api_key),
                )
            OpenaiHelper._openai_clients[model_name] = client
        return OpenaiHelper._openai_clients[model_name]

    @staticmethod
    def call_chat(messages, stat=None, **openai_kwargs):
        rprint(f"Call gpt with openai_kwargs={openai_kwargs}")
        _client = OpenaiHelper.get_openai_client(openai_kwargs["model"])
        chat_completion = _client.chat.completions.create(messages=messages, **openai_kwargs)
        call_return = chat_completion.to_dict()
        update_stat(stat, call_return)
        if "content" not in call_return["choices"][0]["message"]:
            response = ""
        else:
            response = call_return["choices"][0]["message"]["content"]
        if response.strip() == "":
            raise RuntimeError(f"Get empty response from gpt: {call_return}")
        return response
    
class Boto3Helper:
    _boto3_client = {}  # model_name -> Helper

    @staticmethod
    def get_boto3_client(model_name="", region_name = 'us-west-2', api_key="", api_secret_key=""):
        model_name_suffix = f"_{model_name}" if model_name else ""
        if model_name not in Boto3Helper._boto3_client:  # lazy init
            import boto3
            if GET_ENV_VAR("AWS_ACCESS_KEY", f"AWS_ACCESS_KEY{model_name_suffix}") and GET_ENV_VAR("AWS_SECRET_ACCESS_KEY", f"AWS_SECRET_ACCESS_KEY{model_name_suffix}"):
                client = boto3.client("bedrock-runtime", 
                      region_name=GET_ENV_VAR("AWS_REGION_NAME", df=region_name),
                      aws_access_key_id=GET_ENV_VAR("AWS_ACCESS_KEY", f"AWS_ACCESS_KEY{model_name_suffix}", df=api_key),
                    aws_secret_access_key=GET_ENV_VAR("AWS_SECRET_ACCESS_KEY", f"AWS_SECRET_ACCESS_KEY{model_name_suffix}", df=api_secret_key))
            else:
                raise NotImplementedError
            Boto3Helper._boto3_client[model_name] = client
        return Boto3Helper._boto3_client[model_name]
    
    @staticmethod
    def to_bedrock_messages(messages):
        
        def _to_bedrock_message(message):
            import base64
            if isinstance(message["content"], str):
                return [{"text": message["content"]} ]
            else:
                new_message = []
                for item in message["content"]:
                    _type = item['type']
                    if _type == "text":
                        new_message.append({"text": item["text"]})
                    elif _type == "image_url":
                        # 'data:image/{image_suffix};base64'
                        import re
                        pattern = r'data:image/(?P<suffix>[^;]+);base64'
                        match = re.search(pattern, item['image_url']['url'])
                        
                        if match:
                            image_suffix = match.group('suffix')
                        else:
                            raise ValueError("Invalid image URL format")
                        new_message.append({"image": {
                                'format': image_suffix if image_suffix in ['png', 'jpeg', 'webp'] else 'png',
                                "source": {
                                    "bytes": base64.b64decode(item['image_url']['url'][len(f'data:image/{image_suffix};base64,'):] )
                                }
                            }})
                return new_message

        return [
            {"role": message["role"].replace("system", "user"), "content": _to_bedrock_message(message) }
            for message in messages
        ]

    @staticmethod
    def call_chat(messages, stat=None, **boto3_kwargs):
        rprint(f"Call gpt with boto3_kwargs={boto3_kwargs}")
        # import pdb; pdb.set_trace()
        _client = Boto3Helper.get_boto3_client(boto3_kwargs["model"])
        # import pdb; pdb.set_trace()
        # chat_completion = _client.chat.completions.create(messages=messages, **boto3_kwargs)
        messages = Boto3Helper.to_bedrock_messages(messages)
        if boto3_kwargs['thinking']:
            reasoning_config = {
                "thinking": {
                    "type": "enabled",
                    "budget_tokens": 2000
                }
            }
            chat_completion = _client.converse(modelId=GET_ENV_VAR("AWS_MODEL_ID", df="us.anthropic.claude-3-7-sonnet-20250219-v1:0"), messages=messages, additionalModelRequestFields=reasoning_config)
        else:
            chat_completion = _client.converse(modelId=GET_ENV_VAR("AWS_MODEL_ID", df="us.anthropic.claude-3-7-sonnet-20250219-v1:0"), messages=messages)
        
        call_return = chat_completion
        update_stat(stat, call_return)

        content_blocks = call_return["output"]["message"]["content"]

        reasoning = None
        text = None

        # Process each content block to find reasoning and response text
        for block in content_blocks:
            if "reasoningContent" in block:
                reasoning = block["reasoningContent"]["reasoningText"]["text"]
            if "text" in block:
                response = block["text"]
        if reasoning is not None:
            response = f"Reasoning: {reasoning}\n{response}"
            
        if response.strip() == "":
            raise RuntimeError(f"Get empty response from claude: {call_return}")
        return response


class LiteLLMHelper:
    """Routes calls through the LiteLLM SDK so any of LiteLLM's 100+ supported
    providers (OpenAI, Anthropic, Bedrock, Vertex, Cohere, Mistral, Groq,
    Perplexity, Together, Fireworks, Cerebras, ...) are reachable from the
    same `LLM(call_target="litellm:<model>")` shape.

    LiteLLM resolves backing-provider credentials internally per call (e.g.
    `ANTHROPIC_API_KEY` for `anthropic/...`, `OPENAI_API_KEY` for `openai/...`,
    `AWS_*` for `bedrock/...`); no extra env wiring is needed beyond what each
    backing already documents. Environment-variable overrides per call_target
    follow the same convention as OpenaiHelper:

    - `LITELLM_API_KEY[_<call_target_suffix>]`         optional proxy / override key
    - `LITELLM_API_BASE[_<call_target_suffix>]`        optional proxy URL
    - `LITELLM_API_VERSION[_<call_target_suffix>]`     optional (Azure-style)
    """

    @staticmethod
    def call_chat(messages, stat=None, **litellm_kwargs):
        rprint(f"Call litellm with litellm_kwargs={litellm_kwargs}")
        import litellm

        model_name = litellm_kwargs["model"]
        suffix = f"_{model_name}" if model_name else ""

        # Hand explicit api_key / api_base to litellm.completion when set; LiteLLM
        # falls back to backing-provider env vars (ANTHROPIC_API_KEY etc.) otherwise.
        api_key = GET_ENV_VAR("LITELLM_API_KEY", f"LITELLM_API_KEY{suffix}")
        api_base = GET_ENV_VAR("LITELLM_API_BASE", f"LITELLM_API_BASE{suffix}")
        api_version = GET_ENV_VAR("LITELLM_API_VERSION", f"LITELLM_API_VERSION{suffix}")

        call_kwargs = dict(litellm_kwargs)
        if api_key:
            call_kwargs.setdefault("api_key", api_key)
        if api_base:
            call_kwargs.setdefault("api_base", api_base)
        if api_version:
            call_kwargs.setdefault("api_version", api_version)
        # `drop_params=True` lets LiteLLM strip kwargs the chosen backing doesn't
        # accept (e.g. presence_penalty for Anthropic), matching the existing
        # OpenaiHelper's tolerant-of-extras behavior.
        call_kwargs.setdefault("drop_params", True)

        chat_completion = litellm.completion(messages=messages, **call_kwargs)
        call_return = chat_completion.model_dump() if hasattr(chat_completion, "model_dump") else chat_completion.to_dict()
        update_stat(stat, call_return)

        choices = call_return.get("choices") or []
        if not choices:
            raise RuntimeError(f"Get empty choices from litellm: {call_return}")
        message = choices[0].get("message") or {}
        response = message.get("content")
        # Reasoning models (o1, GPT-5, Claude extended-thinking) sometimes put the
        # answer in reasoning_content / thinking when content is empty.
        if not response or not response.strip():
            response = (
                message.get("reasoning_content")
                or message.get("thinking")
                or message.get("output")
                or ""
            )
            if isinstance(response, list):
                parts = []
                for part in response:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        parts.append(part["text"])
                    elif isinstance(part, str):
                        parts.append(part)
                response = "\n\n".join(parts)
        if not isinstance(response, str) or not response.strip():
            raise RuntimeError(f"Get empty response from litellm: {call_return}")
        return response


class LLM(KwargsInitializable):
    def __init__(self, **kwargs):
        # basics
        self.call_target = "manual"  # fake=fake, manual=input, gpt(gpt:model_name)=openai [such as gpt:gpt-4o-mini], request(http...)=request
        self.thinking = False
        self.print_call_in = "white on blue"  # easier to read
        self.print_call_out = "white on green"  # easier to read
        self.max_retry_times = 5  # <0 means always trying
        self.seed = 1377  # zero means no seed!
        # request
        self.request_timeout = 100  # timeout time
        self.max_token_num = 20000
        self.call_kwargs = {"temperature": 0.0, "top_p": 0.95, "max_tokens": 4096}  # other kwargs for gpt/request calling
        # --
        super().__init__(**kwargs)  # init
        # --
        # post init
        self.call_target_type = self.get_call_target_type()
        self.call_stat = {}  # stat of calling
        # --
        self.message_truncator = MessageTruncator()

    def __repr__(self):
        return f"LLM(target={self.call_target},kwargs={self.call_kwargs})"

    def get_seed(self):
        return self.seed

    def set_seed(self, seed):
        self.seed = seed

    def __call__(self, messages, **kwargs):
        func = lambda: self._call_with_messages(messages, **kwargs)
        return wrapped_trying(func, max_times=self.max_retry_times)

    def get_call_stat(self, clear=False):
        ret = self.call_stat.copy()
        if clear:  # clear stat
            self.clear_call_stat()
        return ret

    def clear_call_stat(self):
        self.call_stat.clear()

    def get_call_target_type(self):
        _trg = self.call_target
        if _trg == "manual":
            return "manual"
        elif _trg == "fake":
            return "fake"
        elif _trg.startswith("gpt:"):
            return "gpt"
        elif _trg.startswith("http"):
            return "request"
        elif _trg.startswith("claude:"):
            return "claude"
        elif _trg.startswith("litellm:"):
            return "litellm"
        else:
            raise RuntimeError(f"UNK call_target = {_trg}")

    def show_messages_str(self, messages, calling_kwargs, rprint_style):
        ret_ss = []
        if isinstance(messages, list):
            for one_mesg in messages:
                _content = one_mesg['content']
                if isinstance(_content, list):
                    _content = "\n\n".join([(z['text'] if z['type']=='text' else f"<{str(z)[:150]}...>") for z in _content])
                ret_ss.extend([f"=====\n", (f"{one_mesg['role']}: {_content}\n", rprint_style)])
        else:
            ret_ss.append((f"{messages}\n", rprint_style))
        ret = [f"### ----- Call {self.call_target} with {calling_kwargs} [ctime={time.ctime()}]\n{'#'*10}\n"] + ret_ss + [f"{'#'*10}"]
        return ret

    # still return a str here, for simplicity!
    def _call_with_messages(self, messages, **kwargs):
        time0 = time.perf_counter()
        _call_target_type = self.call_target_type
        _call_kwargs = self.call_kwargs.copy()
        _call_kwargs.update(kwargs)  # this time's kwargs
        if self.print_call_in:
            rprint(self.show_messages_str(messages, _call_kwargs, self.print_call_in))  # print it out
        # --
        if _call_target_type == "manual":
            user_input = input("Put your input >> ")
            response = user_input.strip()
            ret = response
        elif _call_target_type == "fake":
            ret = "You are correct! As long as you are happy!"
        elif _call_target_type == "gpt":
            ret = self._call_openai_chat(messages, **_call_kwargs)
        elif _call_target_type == "claude":
            _call_kwargs['thinking'] = self.thinking or self.thinking == "True"
            ret = self._call_claude_chat(messages, **_call_kwargs)
        elif _call_target_type == "litellm":
            ret = self._call_litellm_chat(messages, **_call_kwargs)
        elif _call_target_type == "request":
            messages = self.message_truncator.truncate_message_list(messages, self.max_token_num)
            headers = {"Content-Type": "application/json"}
            if isinstance(messages, list):
                json_data = {
                    "model": "ck",
                    "stop": ["<|eot_id|>", "<|eom_id|>", "<|im_end|>"],
                    "messages": messages,
                }
                if self.seed != 0:  # only if non-zero!
                    json_data.update(seed=self.seed)
            else:  # directly put it!
                json_data = messages.copy()
            json_data.update(_call_kwargs)
            r = requests.post(self.call_target, headers=headers, json=json_data, timeout=self.request_timeout)
            assert (200 <= r.status_code <= 300), f"response error: {r.status_code} {json_data}"
            call_return = r.json()
            if isinstance(call_return, dict) and "choices" in call_return:
                update_stat(self.call_stat, call_return)
                ret0 = call_return["choices"][0]
                if "message" in ret0:
                    ret = ret0["message"]["content"]  # chat-format
                    # thought = ret0["message"]["reasoning_content"] # for qwen3
                    # remove <think> </think> tokens
                    import re
                    ret = re.sub(r'<think>.*?</think>', '', ret, flags=re.DOTALL)
                else:
                    ret = ret0["text"]
            else:  # directly return the full object
                ret = call_return
        else:
            ret = None
        # --
        assert ret is not None, f"Calling failed for {_call_target_type}"
        if self.print_call_out:
            ss = [f"# == Calling result [ctime={time.ctime()}, interval={time.perf_counter() - time0:.3f}s] =>\n", (ret, self.print_call_out), "\n# =="]
            rprint(ss)
        return ret

    def _call_openai_chat(self, messages, **kwargs):
        _gpt_kwargs = {"model": self.call_target.split(":", 1)[1]}
        _gpt_kwargs.update(kwargs)
        while True:
            try:
                ret = OpenaiHelper().call_chat(messages, stat=self.call_stat, **_gpt_kwargs)
                return ret
            except Exception as e:  # simply catch everything!
                rprint(f"Get error when calling gpt: {e}", style="white on red")
                if type(e).__name__ in ["RateLimitError"]:
                    time.sleep(10)
                elif type(e).__name__ == "BadRequestError":
                    error_str = str(e)
                    if "ResponsibleAIPolicyViolation" in error_str or "content_filter" in error_str:
                        # rprint("Jailbreak or content filter violation detected. Please modify your prompt.", style="white on red")
                        return "Thought: Jailbreak or content filter violation detected. Please modify your prompt or stop with N/A."
                    else:
                        rprint(f"BadRequestError: {error_str}", style="white on red")
                    break
                else:
                    break
        return None

    def _call_litellm_chat(self, messages, **kwargs):
        # `litellm:<model>` -- the model string after the prefix is passed
        # straight to litellm.completion (e.g. `litellm:anthropic/claude-sonnet-4-6`,
        # `litellm:openai/gpt-4o`, `litellm:bedrock/anthropic.claude-3-5-sonnet-...`).
        _litellm_kwargs = {"model": self.call_target.split(":", 1)[1]}
        _litellm_kwargs.update(kwargs)
        while True:
            try:
                ret = LiteLLMHelper.call_chat(messages, stat=self.call_stat, **_litellm_kwargs)
                return ret
            except Exception as e:
                rprint(f"Get error when calling litellm: {e}", style="white on red")
                exc_name = type(e).__name__
                if exc_name in ["RateLimitError", "Timeout"]:
                    time.sleep(10)
                    continue
                if exc_name == "BadRequestError":
                    error_str = str(e)
                    if "ResponsibleAIPolicyViolation" in error_str or "content_filter" in error_str:
                        return "Thought: Jailbreak or content filter violation detected. Please modify your prompt or stop with N/A."
                    rprint(f"BadRequestError: {error_str}", style="white on red")
                break
        return None

    def _call_claude_chat(self, messages, **kwargs):
        _claude_kwargs = {"model": self.call_target.split(":", 1)[1]}
        _claude_kwargs.update(kwargs)
        import botocore
        while True:
            try:
                ret = Boto3Helper().call_chat(messages, stat=self.call_stat, **_claude_kwargs)
                return ret
            # except Exception as e:  # simply catch everything!
            except botocore.exceptions.ClientError as e:
                rprint(f"Get error when calling gpt: {e}", style="white on red")
                if e.response['Error']['Code'] == 'LimitExceededException':
                    time.sleep(10)
                else:
                    return f"Error calling Claude: {e}"
        return None


# --
def test_llm():
    llm = LLM(call_target="gpt:gpt-4o-mini")
    messages = [{"role": "system", "content": "You are a helpful assistant."}]
    while True:
        p = input("Prompt >> ")
        messages.append({"role": "user", "content": p.strip()})
        r = llm(messages)
        messages.append({"role": "assistant", "content": r})

# PYTHONPATH=???/cognitive_kernel_v3/backend python -m ck_pro.agents.model
if __name__ == '__main__':
    test_llm()
