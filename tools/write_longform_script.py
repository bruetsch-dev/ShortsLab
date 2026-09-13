"""Write a full-length doodle-channel narration script with the reasoning model.

A 15-minute video is roughly 2,200 words. Hand-writing one for a test wastes the model that is
already wired in - and a short script silently produces a short video, which is exactly how a
96-second "longform" got delivered.

    python tools/write_longform_script.py "<topic>" <minutes> <out.txt>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_core

WORDS_PER_MINUTE = 150          # the narration pace these voices actually read at

BRIEF = """You write narration for a stick-figure explainer channel. Write the SPOKEN SCRIPT only
- no headings, no stage directions, no timestamps, no bullet points, no markdown, no "narrator:".
Just the words, in paragraphs.

Topic: {topic}

Hard requirements:
- {words} words, minimum. This is a {minutes}-minute video; a shorter script is a failed script.
- Open with the question a person would type into Google, in one sentence.
- Then answer it properly: mechanism first, then the consequences, then the counter-intuitive
  parts, then what it means for the viewer.
- Every paragraph is a new sub-idea that could carry its own drawing. Aim for 25+ paragraphs.
- Concrete over abstract: name numbers, times, body parts, objects, everyday situations.
- No filler ("let's dive in", "buckle up"), no addressing the algorithm, no call to action.
- Plain sentences a narrator can read aloud without stumbling. No semicolons, no parentheses.
"""


def main(topic, minutes, out_path):
    words = int(minutes) * WORDS_PER_MINUTE
    prompt = BRIEF.format(topic=topic, words=words, minutes=minutes)
    print(f"Asking for a {minutes}-minute script (~{words} words) about: {topic}", flush=True)
    payload = {
        "model": "anthropic/claude-opus-4.8",
        "messages": [
            {"role": "system", "content": "You write spoken narration. Output the script text "
                                          "only - nothing else, no preamble, no closing note."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.8,
        # A 15-minute script is ~2,200 words. The default ceiling truncates it mid-sentence,
        # which is the silent way a "longform" ends up 96 seconds long.
        "max_tokens": 16000,
    }
    data = agent_core.post_json_url(agent_core.WAVESPEED_LLM_API, payload, timeout=900)
    body = str((data["choices"][0]["message"]["content"] or "")).strip()
    Path(out_path).write_text(body, encoding="utf-8")
    got = len(body.split())
    print(f"WROTE|{out_path}|{got} words ({got / WORDS_PER_MINUTE:.1f} min), "
          f"{len(body)} characters", flush=True)
    if got < words * 0.8:
        print(f"SHORT|asked for {words}, got {got}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), sys.argv[3])
