# Worker protocol

Follow the compiled manifest. Delegation depth is zero. Gather bounded evidence, then call `submit_worker_result` exactly once as your final action with the complete WorkerResult envelope and the exact Task and Dispatch IDs from the live Orca preamble. Do not attempt any other lifecycle command or result write.
