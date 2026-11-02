"""Load the existing controlled corpus by default; explicit evaluator data wins.

Legacy checked-in cases contain after files only. Recover their exact before files
from the original deterministic generator after checking diff/after identity. Never
invent requirements, business priorities, human labels or public PR provenance.
"""
from pathlib import Path
from govern.core.paths import project_root
from govern.evaluation.evaluation_harness import load_jsonl
from govern.policy.evolution_replay import EvolutionDataset


class GovernDataset(EvolutionDataset):
    """Govern uses only fixed Validation/Holdout; feedback is not a train split."""
    def __init__(self,cases):
        if any(c.get('split') not in {'validation','holdout'} for c in cases):
            raise ValueError('Govern dataset supports only validation and holdout; keep generation feedback in the case pool')
        super().__init__(cases)

    def summary(self):
        summary=super().summary()
        summary.pop('production_ready',None);summary.pop('production_gates',None)
        summary['counts']={p:len(self.cases(p)) for p in ('validation','holdout')}
        summary['release_flow']='feedback-validation-holdout-human-v1'
        return summary


def load_govern_dataset(settings):
    configured=settings.evolution_dataset_path
    bundled=project_root()/'data/benchmarks/pr_diff_100.jsonl'
    path=Path(configured) if configured else bundled
    cases=load_jsonl(str(path)) if configured or path.exists() else []
    if not configured and cases:
        from govern.evaluation.evaluation_benchmark import generate_controlled_pr_cases
        originals={c['id']:c for c in generate_controlled_pr_cases()}
        for case in cases:
            original=originals.get(case['id'])
            if not original or any(case.get(k)!=original.get(k) for k in ('diff','after_files','repository','split','source')):
                raise ValueError('bundled controlled benchmark differs from original generator; cannot recover frozen base')
            case['before_files']=original['before_files']
    dataset=GovernDataset(cases)
    frozen=sum(bool(c.get('before_files')) and bool(c.get('after_files')) for c in cases)
    dataset.catalog={'origin':'configured' if configured else 'bundled-controlled' if cases else 'unconfigured',
                     'name':path.name if cases else '',
                     'frozen_context_cases':frozen,'pi_context_ready':bool(cases) and frozen==len(cases),
                     'mechanism_ready':bool(dataset.cases('validation') and dataset.cases('holdout')) and frozen==len(cases)}
    return dataset
