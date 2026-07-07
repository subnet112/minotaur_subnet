"""Solver submission routes package.

Re-exports the router and all public symbols so that existing imports like
``from minotaur_subnet.api.routes.submissions import router`` and
``from minotaur_subnet.api.routes import submissions`` continue to work.
"""

# Router (registered in server.py)
from .routes import router  # noqa: F401

# State accessors (used by server.py and tests)
from .state import (  # noqa: F401
    get_store,
    set_store,
    get_round_store,
    set_round_store,
    get_epoch_manager,
    set_epoch_manager,
    get_champion_consensus_manager,
    set_champion_consensus_manager,
    get_champion_peer_network,
    set_champion_peer_network,
    get_solver_round_epoch_provider,
    set_solver_round_epoch_provider,
)

# Models (used by server.py for direct construction)
from .models import (  # noqa: F401
    AbortRoundRequest,
    ActivateRoundRequest,
    CertifyRoundRequest,
    ChampionApprovalPayload,
    ChampionConsensusProposalRequest,
    CloseRoundRequest,
    SolverChampionResponse,
    SolverRoundResponse,
    StatusResponse,
    SubmitRequest,
    SubmitResponse,
)

# Round manager functions (used by server.py)
from .round_manager import (  # noqa: F401
    _abort_solver_round_state,
    _close_solver_round_state,
    _sync_round_incumbent_from_submission_store,
    autoscaled_decision_window,
)

# Champion consensus functions (used by server.py)
from .champion_consensus import (  # noqa: F401
    _certify_solver_round_state,
)

# Screening pipeline functions (used by tests)
from .screening_pipeline import (  # noqa: F401
    _build_git_process_env,
    _cleanup_temp_file,
    _clone_repo,
    _run_screening_pipeline,
)

# Signature verification (used by tests via mock paths)
from .routes import (  # noqa: F401
    verify_hotkey_signature,
    build_submission_message,
)

# Round-entry rotation (used by the startup round coordinator at close)
from .routes import (  # noqa: F401
    apply_round_rotation,
)
