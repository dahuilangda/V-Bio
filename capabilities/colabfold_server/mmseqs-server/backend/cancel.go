package main

import (
	"log"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
)

// Ticket cancellation (DELETE /ticket/{id}).
//
// Upstream MMseqs2-App has no way to abandon a search: a client that stops
// polling leaves the job running to completion, holding a full GPU for a
// result nobody will fetch. CancelJob closes that gap:
//
//  1. drop the ticket from the pending queue (no-op when already RUNNING),
//  2. SIGKILL the process group of every process whose command line
//     references the job workdir (the worker runs stages with setpgid, so
//     -pid takes the whole mmseqs pipeline down),
//  3. mark the ticket ERRORED and remove its workdir.
//
// Race safety against the worker: a worker that already dequeued the job
// converges on ERROR too — killed stages return JobExecutionError, and any
// later step fails fast because the workdir is gone. A job that genuinely
// finished between the status check and the kill keeps COMPLETE (its result
// stays fetchable), which is the honest outcome for a lost race.

func CancelJob(jobsystem JobSystem, config ConfigRoot, id Id) (Status, error) {
	ticket, err := jobsystem.GetTicket(id)
	if err != nil {
		return StatusError, err
	}
	switch ticket.RawStatus {
	case StatusPending, StatusRunning:
	case StatusComplete:
		return StatusComplete, nil
	default:
		return ticket.RawStatus, nil
	}

	workdir := filepath.Join(config.Paths.Results, string(id))
	// Path safety for the /proc scan and RemoveAll below: the ticket id is
	// validated by GetTicket (base64-ish, no separators), this is defense in depth.
	if !strings.HasPrefix(workdir, filepath.Clean(config.Paths.Results)+string(os.PathSeparator)) {
		return StatusError, os.ErrInvalid
	}

	if queue, ok := jobsystem.(interface {
		RemovePending(Id) error
	}); ok {
		if err := queue.RemovePending(id); err != nil {
			log.Printf("cancel %s: pending-queue removal failed: %s\n", id, err)
		}
	}

	killed := killProcessesOfJob(workdir)
	if err := jobsystem.SetStatus(id, StatusError); err != nil {
		return StatusError, err
	}
	if err := os.RemoveAll(workdir); err != nil {
		log.Printf("cancel %s: workdir removal failed: %s\n", id, err)
	}
	log.Printf("cancelled ticket %s (killed %d process(es))\n", id, killed)
	return StatusError, nil
}

// killProcessesOfJob SIGKILLs every process whose command line mentions the
// job workdir. Returns the number of processes signalled.
func killProcessesOfJob(workdir string) int {
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return 0
	}
	self := os.Getpid()
	killed := 0
	for _, entry := range entries {
		pid, err := strconv.Atoi(entry.Name())
		if err != nil || pid == self {
			continue
		}
		raw, err := os.ReadFile(filepath.Join("/proc", entry.Name(), "cmdline"))
		if err != nil {
			continue
		}
		cmdline := strings.ReplaceAll(string(raw), "\x00", " ")
		if !strings.Contains(cmdline, workdir) {
			continue
		}
		// -pid signals the process group (stages run with setpgid); the plain
		// pid signal covers processes that somehow lead their own new group.
		if err := syscall.Kill(-pid, syscall.SIGKILL); err == nil {
			killed++
		} else if err := syscall.Kill(pid, syscall.SIGKILL); err == nil {
			killed++
		}
	}
	return killed
}
