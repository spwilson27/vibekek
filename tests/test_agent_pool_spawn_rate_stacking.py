import time
import threading
from workflow_lib.agent_pool import AgentConfig, AgentPoolManager

def test_spawn_rate_stacking():
    cfg = AgentConfig("test", "gemini", "user", parallel=5, priority=1, quota_time=60, spawn_rate=0.5)
    pool = AgentPoolManager([cfg])

    def worker(i, results):
        start = time.time()
        agent = pool.acquire(timeout=5.0)
        end = time.time()
        results[i] = end - start
        
    results = [0, 0, 0]
    threads = []
    
    t0 = time.time()
    for i in range(3):
        t = threading.Thread(target=worker, args=(i, results))
        threads.append(t)
        t.start()
        
    for t in threads:
        t.join()
        
    results.sort()
    
    # 0th agent: immediately
    assert results[0] < 0.2
    # 1st agent: delayed by ~0.5s
    assert results[1] >= 0.4
    # 2nd agent: delayed by ~1.0s
    assert results[2] >= 0.9

if __name__ == "__main__":
    test_spawn_rate_stacking()
    print("Passed!")
