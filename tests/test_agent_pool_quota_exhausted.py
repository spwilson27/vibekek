import time
import threading
from workflow_lib.agent_pool import AgentConfig, AgentPoolManager

def test_spawn_rate_quota_exhaust():
    cfg1 = AgentConfig("test1", "gemini", "user", parallel=2, priority=1, quota_time=60, spawn_rate=2.0)
    cfg2 = AgentConfig("test2", "gemini", "user", parallel=2, priority=2, quota_time=60)
    pool = AgentPoolManager([cfg1, cfg2])

    def worker(results):
        agent = pool.acquire(timeout=5.0)
        results.append(agent.name if agent else None)

    results = []
    
    # First acquire gets test1 immediately
    a1 = pool.acquire(timeout=1.0)
    assert a1.name == "test1"
    
    # Second acquire should get test1, but block for 2.0s
    t = threading.Thread(target=worker, args=(results,))
    t.start()
    
    time.sleep(0.5)
    
    # While second acquire is waiting, quota gets exhausted
    pool.release(a1, quota_exhausted=True)
    
    t.join()
    
    # The waiting thread should have noticed the quota and picked test2 instead of sleeping!
    print("Agent picked by waiting thread:", results[0])

if __name__ == "__main__":
    test_spawn_rate_quota_exhaust()
    print("Passed!")
