import time
import threading
from collections import deque

class RateLimiter:
    def __init__(self):
        # Development API limits:
        # 20 requests every 1 second
        # 100 requests every 2 minutes (120 seconds)
        self.short_window_limit = 20
        self.short_window_seconds = 1.0
        
        self.long_window_limit = 100
        self.long_window_seconds = 120.0
        
        self.short_queue = deque()
        self.long_queue = deque()
        
        # Thread-safe lock: critical for multi-threading
        self.lock = threading.Lock()
        
    def wait(self):
        with self.lock:
            now = time.time()
            
            # Clean up old entries
            while self.short_queue and self.short_queue[0] <= now - self.short_window_seconds:
                self.short_queue.popleft()
                
            while self.long_queue and self.long_queue[0] <= now - self.long_window_seconds:
                self.long_queue.popleft()
                
            # Check short window
            if len(self.short_queue) >= self.short_window_limit:
                sleep_time = self.short_queue[0] + self.short_window_seconds - now
                if sleep_time > 0:
                    time.sleep(sleep_time)
                    now = time.time()
                    
            # Check long window
            if len(self.long_queue) >= self.long_window_limit:
                sleep_time = self.long_queue[0] + self.long_window_seconds - now
                if sleep_time > 0:
                    time.sleep(sleep_time)
                    now = time.time()
                    
            # Add current timestamp
            self.short_queue.append(now)
            self.long_queue.append(now)

# Global instance (shared across threads)
limiter = RateLimiter()
