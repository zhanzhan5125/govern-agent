"""Build a bounded set of declarative alternatives from one verified proposal."""
import copy


class CandidateBuilder:
    def build(self, kind, baseline, generated, maximum=3):
        proposed = str(generated.get("candidate_prompt", "")).strip()
        if not proposed:
            raise ValueError("proposer returned an empty change")
        base_prompt = str(baseline.get("prompt", "")) if kind == "prompt" else ""
        if kind == "prompt" and proposed == base_prompt.strip():
            return []
        guard = ("\nRequire current changed-line evidence before reporting each learned pattern; "
                 "check counterexamples and preserve existing tool permissions and review budgets.")
        texts = [proposed, proposed + guard]
        results = []
        for index, text in enumerate(texts[:min(3, maximum)]):
            if kind == "prompt":
                content = {"prompt": text}
            elif kind == "skill":
                content = copy.deepcopy(baseline)
                content["files"]["SKILL.md"] = content["files"]["SKILL.md"].rstrip() + (
                    "\n\n## Candidate review guidance\n\n" + text + "\n")
            else:
                raise ValueError("unsupported candidate kind")
            results.append({"variant": "minimal" if index == 0 else "evidence-first", "content": content})
        return results
