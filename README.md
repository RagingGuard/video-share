# video-share

a lightweight video-sharing app on a local network


The program runs as a service on a Windows PC (or a server) within a local network. To avoid affecting other tasks on the machine, it’s optimized to stay lightweight when idle, and it allocates resources dynamically as needed.

因为程序是局域网内windows电脑（也可以是服务器）上打开的服务，为了不影响电脑的其他功能，所以针对静默状态做了优化，资源的使用也是动态分配的。



Because of this design, the program is quite small and isn’t meant for heavy concurrent use. I’ve set a limit of 100 clients. Even if all 100 clients connect at the same time, the system will only scale up to 48 threads and won’t go beyond that. In extreme cases like this, as long as users aren’t frequently scrubbing the timeline or switching videos, the system can still run fairly smoothly.

因为程序是局域网内windows电脑（也可以是服务器）上打开的服务，为了不影响电脑的其他功能，所以针对静默状态做了优化，资源的使用也是动态分配的。也正是因为如此，程序规模很小，这个程序不能给很多人同时使用，我设置了一个100个客户端的限制。如果真的到了100个客户端同时访问，线程也会开到48个，但线程也不会再多了，这种极限情况只要客户端不进行频繁拖拽和切换视频，系统运行也能比较顺畅。
