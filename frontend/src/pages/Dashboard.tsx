import { useEffect, useState } from "react";

interface User {
  user_id: number;
}

interface Service {
  id: number;
  name: string;
  description: string | null;
  average_service_time: number;
  is_active: boolean;
}

function Dashboard() {
  const [user, setUser] = useState<User | null>(null);
  const [services, setServices] = useState<Service[]>([]);
  const [loading, setLoading] = useState(true);

  const [joiningService, setJoiningService] = useState<number | null>(null);
  const [cancellingQueue, setCancellingQueue] = useState(false);

  const [queueToken, setQueueToken] = useState<{
    id: number;
    token_number: number;
    queue_id: number;
    status: string;
  } | null>(null);

  const [queuePosition, setQueuePosition] = useState<number | null>(null);
  const [estimatedWait, setEstimatedWait] = useState<number | null>(null);
  const [currentToken, setCurrentToken] = useState<number | null>(null);

  const [queueError, setQueueError] = useState("");

  const token = localStorage.getItem("access_token");

  // =========================
  // JOIN QUEUE
  // =========================
  const handleJoinQueue = async (serviceId: number) => {
    if (!token) {
      setQueueError("Please login again.");
      return;
    }

    setJoiningService(serviceId);
    setQueueError("");

    try {
      // Find today's active queue
      const queueResponse = await fetch(
        `http://127.0.0.1:8000/queues/service/${serviceId}`
      );

      const queueData = await queueResponse.json();

      if (!queueResponse.ok) {
        throw new Error(
          queueData.detail || "No active queue available"
        );
      }

      // Create customer token
      const tokenResponse = await fetch(
        "http://127.0.0.1:8000/tokens",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${token}`,
          },
          body: JSON.stringify({
            queue_id: queueData.queue_id,
          }),
        }
      );

      const tokenData = await tokenResponse.json();

      if (!tokenResponse.ok) {
        throw new Error(
          tokenData.detail || "Unable to join queue"
        );
      }

      // Store queue token
      setQueueToken({
        id: tokenData.id,
        token_number: tokenData.token_number,
        queue_id: tokenData.queue_id,
        status: tokenData.status,
      });

      // Initial position
      setQueuePosition(1);
    } catch (error) {
      setQueueError(
        error instanceof Error
          ? error.message
          : "Something went wrong"
      );
    } finally {
      setJoiningService(null);
    }
  };

  // =========================
  // LEAVE QUEUE
  // =========================
  const handleCancelQueue = async () => {
    if (!queueToken || !token) {
      return;
    }

    setCancellingQueue(true);
    setQueueError("");

    try {
      const response = await fetch(
        `http://127.0.0.1:8000/tokens/${queueToken.id}/cancel`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
          },
        }
      );

      const data = await response.json();

      if (!response.ok) {
        throw new Error(
          data.detail || "Unable to leave queue"
        );
      }

      // Clear current queue information
      setQueueToken(null);
      setQueuePosition(null);
      setEstimatedWait(null);
      setCurrentToken(null);
    } catch (error) {
      setQueueError(
        error instanceof Error
          ? error.message
          : "Something went wrong"
      );
    } finally {
      setCancellingQueue(false);
    }
  };

  // =========================
  // LOAD DASHBOARD
  // =========================
  useEffect(() => {
    const loadDashboard = async () => {
      try {
        const [profileResponse, servicesResponse] =
          await Promise.all([
            fetch("http://127.0.0.1:8000/profile", {
              headers: {
                Authorization: `Bearer ${token}`,
              },
            }),

            fetch("http://127.0.0.1:8000/services"),
          ]);

        if (profileResponse.ok) {
          const profileData = await profileResponse.json();
          setUser(profileData);
        }

        if (servicesResponse.ok) {
          const servicesData = await servicesResponse.json();
          setServices(servicesData);
        }
      } catch (error) {
        console.error(
          "Dashboard loading failed:",
          error
        );
      } finally {
        setLoading(false);
      }
    };

    loadDashboard();
  }, [token]);

  // =========================
  // LOAD QUEUE POSITION
  // =========================
  useEffect(() => {
    if (!queueToken) {
      return;
    }

    const loadQueuePosition = async () => {
      try {
        const response = await fetch(
          `http://127.0.0.1:8000/tokens/${queueToken.id}/position`
        );

        const data = await response.json();

        if (response.ok) {
          setQueuePosition(data.position);

          const service = services.find(
            (service) => service.id === data.service_id
          );

          if (service) {
            setEstimatedWait(
              (data.position - 1) *
                service.average_service_time
            );
          }
        }
      } catch (error) {
        console.error(
          "Queue position loading failed:",
          error
        );
      }
    };

    loadQueuePosition();
  }, [queueToken, services]);

  // =========================
  // WEBSOCKET
  // =========================
  useEffect(() => {
    if (!queueToken) {
      return;
    }

    const ws = new WebSocket(
      `ws://127.0.0.1:8000/ws/queues/${queueToken.queue_id}?token=${encodeURIComponent(
        token || ""
      )}`
    );

    ws.onopen = () => {
      console.log("Queue WebSocket connected");
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);

        console.log("Queue update:", data);

        if (data.current_token !== undefined) {
          setCurrentToken(data.current_token);

          // Refresh customer's queue position
          if (queueToken) {
            fetch(
              `http://127.0.0.1:8000/tokens/${queueToken.id}/position`
            )
              .then((response) => response.json())
              .then((positionData) => {
                if (positionData.position !== undefined) {
                  setQueuePosition(positionData.position);

                  const service = services.find(
                    (service) =>
                      service.id ===
                      positionData.service_id
                  );

                  if (service) {
                    setEstimatedWait(
                      (positionData.position - 1) *
                        service.average_service_time
                    );
                  }
                }
              })
              .catch((error) => {
                console.error(
                  "Position refresh failed:",
                  error
                );
              });
          }
        }
      } catch (error) {
        console.error(
          "WebSocket message error:",
          error
        );
      }
    };

    ws.onerror = (error) => {
      console.error(
        "Queue WebSocket error:",
        error
      );
    };

    ws.onclose = () => {
      console.log(
        "Queue WebSocket disconnected"
      );
    };

    return () => {
      ws.close();
    };
  }, [queueToken, token, services]);

  // =========================
  // LOADING
  // =========================
  if (loading) {
    return (
      <div className="min-h-screen bg-slate-950 flex items-center justify-center">
        <p className="text-slate-400">
          Loading QueueLess...
        </p>
      </div>
    );
  }

  // =========================
  // UI
  // =========================
  return (
    <div className="min-h-screen bg-slate-950 text-white">

      {/* Header */}
      <header className="border-b border-slate-800 bg-slate-900/80">
        <div className="max-w-7xl mx-auto px-6 py-4 flex items-center justify-between">

          <div>
            <h1 className="text-2xl font-bold">
              QueueLess
            </h1>

            <p className="text-xs text-slate-400">
              Smart Queue Management
            </p>
          </div>

          <button
            onClick={() => {
              localStorage.removeItem(
                "access_token"
              );

              window.location.href = "/login";
            }}
            className="px-4 py-2 rounded-lg border border-slate-700 text-slate-300 hover:bg-slate-800 transition"
          >
            Logout
          </button>

        </div>
      </header>

      {/* Main */}
      <main className="max-w-7xl mx-auto px-6 py-10">

        {/* Welcome */}
        <section className="mb-10">

          <p className="text-blue-400 text-sm font-medium">
            Welcome back
          </p>

          <h2 className="text-4xl font-bold mt-2">
            Your queue, without the waiting.
          </h2>

          <p className="text-slate-400 mt-3 max-w-2xl">
            Choose a service and manage your queue
            digitally. You can track your position
            and estimated waiting time in real time.
          </p>

          {user && (
            <p className="text-xs text-slate-600 mt-3">
              User ID: {user.user_id}
            </p>
          )}

        </section>

        {/* Current Queue */}
        <section className="mb-10">

          <div className="flex items-center justify-between mb-4">
            <h3 className="text-xl font-semibold">
              Current Queue
            </h3>
          </div>

          <div className="bg-slate-900 border border-slate-800 rounded-2xl p-8">

            {queueToken ? (

              <div className="text-center py-6">

                <p className="text-blue-400 text-sm font-medium">
                  Your Queue Token
                </p>

                <div className="text-6xl font-bold mt-3">
                  #{queueToken.token_number}
                </div>

                <p className="text-slate-400 mt-3">
                  Queue ID: {queueToken.queue_id}
                </p>

                <div className="inline-flex mt-4 px-4 py-2 rounded-full bg-yellow-500/10 text-yellow-400 text-sm">
                  {queueToken.status}
                </div>

                {queuePosition !== null && (
                  <p className="text-slate-300 mt-5">
                    Your position:{" "}
                    <span className="font-semibold text-white">
                      {queuePosition}
                    </span>
                  </p>
                )}

                {estimatedWait !== null && (
                  <p className="text-slate-300 mt-3">
                    Estimated wait:{" "}
                    <span className="font-semibold text-white">
                      ~{estimatedWait} minutes
                    </span>
                  </p>
                )}

                {/* Leave Queue */}
                <button
                  onClick={handleCancelQueue}
                  disabled={cancellingQueue}
                  className="mt-6 px-5 py-2 rounded-lg border border-red-500/40 text-red-400 hover:bg-red-500/10 disabled:opacity-50 text-sm font-semibold transition"
                >
                  {cancellingQueue
                    ? "Leaving..."
                    : "Leave Queue"}
                </button>

              </div>

            ) : (

              <div className="flex flex-col items-center justify-center text-center py-8">

                <div className="w-16 h-16 rounded-full bg-blue-500/10 flex items-center justify-center mb-4">
                  <span className="text-2xl">
                    +
                  </span>
                </div>

                <h4 className="text-lg font-semibold">
                  You are not in a queue
                </h4>

                <p className="text-slate-400 text-sm mt-2">
                  Select a service below to join a queue.
                </p>

              </div>

            )}

            {queueError && (
              <div className="mt-4 rounded-lg bg-red-500/10 border border-red-500/30 px-4 py-3 text-sm text-red-400">
                {queueError}
              </div>
            )}

          </div>

        </section>

        {/* Services */}
        <section>

          <div className="mb-5">

            <h3 className="text-xl font-semibold">
              Available Services
            </h3>

            <p className="text-slate-400 text-sm mt-1">
              Choose the service you need.
            </p>

          </div>

          {services.length === 0 ? (

            <div className="bg-slate-900 border border-slate-800 rounded-2xl p-8 text-center">

              <p className="text-slate-400">
                No services available.
              </p>

            </div>

          ) : (

            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">

              {services
                .filter(
                  (service) => service.is_active
                )
                .map((service) => (

                  <div
                    key={service.id}
                    className="bg-slate-900 border border-slate-800 rounded-2xl p-6 hover:border-blue-500/50 transition"
                  >

                    <div className="w-11 h-11 rounded-xl bg-blue-500/10 flex items-center justify-center mb-5">

                      <span className="text-blue-400 font-bold">
                        {service.name.charAt(0)}
                      </span>

                    </div>

                    <h4 className="text-lg font-semibold">
                      {service.name}
                    </h4>

                    <p className="text-slate-400 text-sm mt-2 min-h-10">
                      {service.description ||
                        "Service available through QueueLess."}
                    </p>

                    <div className="flex items-center justify-between mt-6">

                      <span className="text-sm text-slate-400">
                        ~{service.average_service_time} min
                      </span>

                      <button
                        onClick={() =>
                          handleJoinQueue(
                            service.id
                          )
                        }
                        disabled={
                          joiningService ===
                          service.id
                        }
                        className="px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-sm font-semibold transition"
                      >
                        {joiningService ===
                        service.id
                          ? "Joining..."
                          : "Join Queue"}
                      </button>

                    </div>

                  </div>

                ))}

            </div>

          )}

        </section>

      </main>

    </div>
  );
}

export default Dashboard;