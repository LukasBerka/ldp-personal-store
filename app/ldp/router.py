"""LDP HTTP layer: RDF resource and container endpoints over the storage backend.

Handlers are synchronous: the backend performs blocking rdflib, lock, and
filesystem work, and FastAPI runs sync path operations in a threadpool, which is
the correct execution model for blocking code.
"""

from fastapi import APIRouter

router = APIRouter(tags=["ldp"])
