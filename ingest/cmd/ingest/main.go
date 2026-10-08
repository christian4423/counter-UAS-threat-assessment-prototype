package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/bluenviron/gomavlib/v4"
)

func main() {
	// 1. Define command-line flags for Docker Compose or systemd configuration
	listenAddr := flag.String("listen", "0.0.0.0:14550", "UDP address to listen on for incoming MAVLink packets")
	forwardAddr := flag.String("forward", "127.0.0.1:14551", "UDP address to forward incoming MAVLink packets to")
	flag.Parse()

	log.Printf("Starting MAVLink proxy...")
	log.Printf("Listening on: %s", *listenAddr)
	log.Printf("Forwarding to: %s", *forwardAddr)

	// 2. Configure the Node layout
	node := &gomavlib.Node{
		Endpoints: []gomavlib.Endpoint{
			&gomavlib.EndpointUDPServer{Address: *listenAddr},
			&gomavlib.EndpointUDPClient{Address: *forwardAddr},
		},
		OutVersion:       gomavlib.V2,
		OutSystemID:      253,
		HeartbeatDisable: true,
	}

	// 3. Initialize the Node
	err := node.Initialize()
	if err != nil {
		log.Fatalf("Initialization error: %v", err)
	}

	// 4. Set up context tied to system termination signals
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// 5. Spin up a goroutine that waits on signal cancellation to trigger node closure
	go func() {
		<-ctx.Done()
		log.Println("Termination signal received. Closing MAVLink node...")
		node.Close() // Closes transport loops and terminates the node.Events() channel
	}()

	log.Println("Proxy event loop started. Waiting for packets...")

	// 6. Range over the events channel. It safely drops out of the loop once node.Close() runs.
	// Frames are counted and summarized once per second, or every 500 frames, whichever comes first.
	const summaryEvery = 500
	frames := 0
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	logSummary := func() {
		if frames > 0 {
			log.Printf("[Frames] %d frames since last summary", frames)
			frames = 0
		}
	}

	events := node.Events()
loop:
	for {
		select {
		case <-ticker.C:
			logSummary()
		case evt, ok := <-events:
			if !ok {
				break loop
			}
			switch e := evt.(type) {
			case *gomavlib.EventFrame:
				frames++
				if frames >= summaryEvery {
					logSummary()
					ticker.Reset(time.Second)
				}

				// Forward the frame to all other endpoints connected to this node
				// (Because forwardAddr is defined in the endpoints slice, node.WriteFrameExcept routes it automatically)
				node.WriteFrameExcept(e.Channel, e.Frame)
			case *gomavlib.EventChannelOpen:
				log.Printf("MAVLink Channel opened: %v", e.Channel)

			case *gomavlib.EventChannelClose:
				log.Printf("MAVLink Channel closed: %v", e.Channel)
			case *gomavlib.EventParseError:
				log.Printf("MAVLink Parsing error: %v", e.Channel)
			}
		}
	}
	logSummary()

	log.Println("Proxy service cleanly shut down. Goodbye!")
}
