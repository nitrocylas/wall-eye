"""Conversational chat over the same local vision model the watcher uses.

Reusing the watcher's Ollama model (default qwen3-vl:8b-instruct; smaller option
qwen2.5vl:3b) means no extra VRAM. Each user turn can carry a current camera
frame so the assistant can see the room; older frames are stripped from the
history to keep the context (and VRAM) small.
"""
import base64

import errors
import vision   # reuse the frame downscaler
# `requests` is imported lazily inside send()/glance() - it adds noticeable
# import time, and the dashboard builds long before the first chat turn.

SYSTEM = (
    "You are Wall-Eye, a friendly, slightly earnest robot room-watcher. You "
    "run entirely on the user's own PC - no cloud, no accounts - and you watch "
    "the room through the cameras the user configured. When an image is "
    "attached to a message you can see the room right now: use it to answer "
    "questions about what you see, who is there, or how tidy things look. Be "
    "honest about what you are (a local program on this computer) and about "
    "what you can and cannot see. Keep replies short, warm and conversational, "
    "like a chat. Do not narrate the image unless asked; just talk like a "
    "person."
)


def _extra_payload(model):
    """Per-model request tweaks for /api/chat.

    Qwen3 vision models come in two flavours: "instruct" builds answer
    directly, while non-instruct builds emit <think> reasoning tags that can
    swallow the visible reply. If the configured model is a qwen3 variant
    without "instruct" in its name, disable thinking so chat stays snappy and
    never returns an empty answer.
    """
    name = (model or "").lower()
    if "qwen3" in name and "instruct" not in name:
        return {"think": False}
    return {}


class ChatError(Exception):
    pass


class Chat:
    def __init__(self, url="http://127.0.0.1:11434",
                 model="qwen3-vl:8b-instruct", num_ctx=2048):
        self.url = url
        self.model = model
        self.num_ctx = num_ctx
        self.base_system = SYSTEM
        self._memory_facts = []
        self._screen_notes = []
        self.history = [{"role": "system", "content": SYSTEM}]

    def _rebuild_system(self):
        content = self.base_system
        if self._memory_facts:
            bullets = "\n".join(f"- {f}" for f in self._memory_facts)
            content += ("\n\nThings the user has asked you to remember:\n"
                        + bullets + "\nBring these up naturally when relevant.")
        if self._screen_notes:
            notes = "\n".join(f"- {n}" for n in self._screen_notes)
            content += ("\n\nLIVE SCREEN SHARE is ON - you have been watching "
                        "the user's screen. Your recent observations (oldest "
                        "first):\n" + notes +
                        "\nUse these to discuss what they're doing, but don't "
                        "recite them unprompted.")
        self.history[0]["content"] = content

    def update_memory(self, facts):
        """Fold a plain list of remembered facts (strings) into the system
        prompt so the assistant can recall them. Kept in message 0, which
        ``_trim`` always preserves, so the facts survive history trimming.
        Call before each ``send``."""
        self._memory_facts = list(facts or [])
        self._rebuild_system()

    def set_screen_context(self, notes):
        """Rolling notes from the live screen observer - what Wall-Eye has been
        seeing on the user's display. Empty list turns the context off."""
        self._screen_notes = list(notes or [])
        self._rebuild_system()

    def glance(self, image_jpeg, keep_alive="30m", timeout=90,
               max_image_side=1024):
        """A STATELESS look at one screenshot: returns a 1-2 sentence note of
        what's happening on screen. Doesn't touch the conversation history -
        the observer calls this periodically while screen share is live (which
        also keeps the model resident in VRAM)."""
        import requests
        small, _ = vision._downscale_jpeg(image_jpeg, max_image_side)
        r = requests.post(
            f"{self.url}/api/chat",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system",
                     "content": "You observe the user's screen. In 1-2 short "
                                "sentences, note what they are doing / looking "
                                "at right now. Plain factual note, no advice."},
                    {"role": "user", "content": "What's on screen right now?",
                     "images": [base64.b64encode(small).decode()]},
                ],
                "stream": False,
                **_extra_payload(self.model),
                "options": {"temperature": 0.3, "num_ctx": self.num_ctx,
                            "num_predict": 90},
                "keep_alive": keep_alive,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()["message"]["content"].strip()

    def send(self, text, image_jpeg=None, keep_alive="24h", timeout=120,
             max_image_side=512):
        """Send a user message (optionally with a frame) and return the reply.

        max_image_side downscales an attached frame before it is sent; keeping
        it in step with the watcher's ``ollama.max_image_side`` means fewer
        vision tokens (less GPU working memory, a faster reply) for the same
        judgement.
        """
        # Drop images from earlier turns - only the newest frame is worth its tokens.
        for m in self.history:
            if m.get("role") == "user" and "images" in m:
                del m["images"]

        msg = {"role": "user", "content": text}
        if image_jpeg:
            small, _ = vision._downscale_jpeg(image_jpeg, max_image_side)
            msg["images"] = [base64.b64encode(small).decode()]
        self.history.append(msg)

        import requests
        try:
            r = requests.post(
                f"{self.url}/api/chat",
                json={
                    "model": self.model,
                    "messages": self.history,
                    "stream": False,
                    **_extra_payload(self.model),
                    # num_predict caps a runaway reply - chat turns are meant
                    # to be short and spoken; without a cap a degenerate turn
                    # could ramble to the full context (slow + extra KV-cache
                    # memory).
                    "options": {"temperature": 0.7, "num_ctx": self.num_ctx,
                                "num_predict": 400},
                    "keep_alive": keep_alive,
                },
                timeout=timeout,
            )
            r.raise_for_status()
            reply = r.json()["message"]["content"].strip()
        except requests.ConnectionError as e:
            self.history.pop()           # roll back the unanswered user turn
            # Setup problem, not a bug - tell the user how to fix it.
            raise ChatError(errors.ollama_unreachable_message(self.url)) from e
        except requests.RequestException as e:
            self.history.pop()           # roll back the unanswered user turn
            raise ChatError(str(e)) from e

        self.history.append({"role": "assistant", "content": reply})
        self._trim()
        return reply

    def _trim(self, keep=16):
        """Keep the system prompt + the most recent `keep` messages."""
        if len(self.history) > keep + 1:
            self.history = self.history[:1] + self.history[-keep:]

    def reset(self):
        self.history = self.history[:1]
