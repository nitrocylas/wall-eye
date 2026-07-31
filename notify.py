"""Alert delivery: Windows toast + optional free phone push via ntfy.sh."""
from winotify import Notification, audio

from paths import APP_NAME

APP_ID = APP_NAME


def toast(title, message):
    n = Notification(
        app_id=APP_ID,
        title=title,
        msg=message[:200],
        duration="long",
    )
    n.set_audio(audio.Default, loop=False)
    n.show()


def ntfy(topic, title, message, priority="default"):
    """Free push to your phone: install the ntfy app, subscribe to the topic.

    Pick an unguessable topic name (it's the only 'password').
    """
    if not topic:
        return
    import requests   # deferred: importing requests costs ~0.9s at startup and
                      # phone push is rarely the first thing that runs
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": "camera"},
            timeout=10,
        )
    except Exception:
        pass  # phone push is best-effort; the toast already fired


def alert(title, message, ntfy_topic="", priority="default"):
    toast(title, message)
    ntfy(ntfy_topic, title, message, priority)
