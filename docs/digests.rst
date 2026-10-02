Digests
=======

.. warning::
   Digests are **experimental**. The configuration, the email format
   and the defaults may change in a future release. Please tell us what
   works and what doesn't at tools@kernel.org.

A busy list like LKML gets hundreds of messages a day. You may not want
all of them in your inbox, but you still want to know what is going on.
A **digest** is one email per day (or per week) that lists the threads
on a list. From each thread you can jump to the archive, or get the
whole thread into your mailbox with one command.

A digest can also have a short **summary** of each thread, written by a
language model that you choose. This part is optional, and it is the
most experimental part.

This page shows how to set up digests and what to do when something goes
wrong. All the configuration keys are listed in "Digest Deliveries" and
"Summarized Digests" in :doc:`configuration`.

What a Digest Looks Like
------------------------

Here is the start of a daily digest with summaries, as plain text. The
HTML part has the same content.

.. code-block:: text

   lkml digest
   2026-09-30 07:00 to 2026-10-01 07:00
   3 threads, 8 messages (2 new, 1 continuing)

   ========================================================================
   NEW PATCHES AND PULL REQUESTS (2)
   ========================================================================
   [PATCH v3 0/2] mm: frobnicate the widgets
     new | 5 new messages | 3 people | v3 | 2 patches posted
     + Reviewed-by: Bob Dev <bob@example.org>
     + Acked-by: Famous Person <famous@example.org> (sent by troll@example.org)

     Summary (machine-generated):
       - The series replaces a list walk with a tail pointer, so that
         adding a widget no longer takes longer as the list grows.
       - Bob Dev is happy with patch 1.

     Posted by P. Author, Wed 09:12:
       1/2  mm: add a tail pointer
       2/2  mm: use the tail pointer
     Follow-ups:
       Wed 14:30  Bob Dev  on 1/2 | Reviewed-by
       Wed 15:00  Troll  Acked-by
     Read:  https://lore.kernel.org/lkml/cover@x/

   ------------------------------------------------------------------------
   [RFC] net: a new idea
     ...

   ========================================================================
   DISCUSSIONS (1)
   ========================================================================
   mm: why is this slow?
     continuing | 2 new messages | 2 people

     Summary (machine-generated):
       - Carol asks why widget lookup got slower.
       - The author points to the new locking.
     ...

Some things to notice:

* The threads come in up to four sections, so you can start with the
  work that waits for you:

  * **New patches and pull requests**: threads where patches were posted
    in this period, even as a reply (like a v2 sent in reply to v1), and
    new pull requests.
  * **Updates to earlier patches**: replies to patches and pull requests
    that were posted before this period.
  * **Bug reports**: threads with "bug" in the subject, like
    ``[BUG] mm: oops in frob()``. A thread where somebody posts a fix
    goes with the new patches instead.
  * **Discussions**: everything else.

  In each section, the busiest threads come first.
* The line under the subject and the trailers are **facts**. korgalore
  counts them itself, so they are always right, with or without a
  model.
* A ``+`` marks each new trailer, as in b4. A trailer that somebody
  else sent for the person it names gets a warning, like the
  ``Acked-by`` above. In the HTML part, the trailers are in a gray box
  with a fixed-width font, so you can find them quickly.
* The summary is marked "machine-generated". It can be wrong: read the
  thread before you act on it. The model is asked for a short list of
  points. In the HTML part, a gray bar on the left sets the summary
  apart from the facts.
* A patch series is listed once, in order. "Follow-ups" has the replies.
  A reply to a patch says which patch, like ``on 1/2``.
* A thread is named after its first message, usually the cover letter.
  When reviewers answer an older series, the cover letter is not in
  the period. Then korgalore fetches it from lore.kernel.org, so the
  thread does not get the name of one patch, like ``[PATCH v3 2/7]``.
  This only works for lore feeds. If lore does not answer, korgalore
  stops asking for this digest, and those threads keep the name of
  their oldest message in the period.
* To get the whole thread into your mailbox, copy the thread's link and
  run ``kgl yank -T <link>``. ``kgl track add <link>`` also delivers the
  replies that come later. See :doc:`usage`.

Plain Digests
-------------

A plain digest has no summaries, so it needs no language model. Start
here, even if you want summaries later.

Add a delivery with ``mode = 'digest'``:

.. code-block:: toml

   [deliveries.lkml-digest]
   feed = 'lkml'
   target = 'personal'
   mode = 'digest'
   schedule = 'daily'
   send_at = '07:00'

Then send the first one right away to see what it looks like:

.. code-block:: bash

   kgl digest --force lkml-digest

After that, ``kgl pull`` (or the GUI) sends the digest each day, on its
first run after 07:00. Run ``kgl pull`` often, for example every 15
minutes from a timer (see "Automated Pulls" in :doc:`usage`).

Summarized Digests
------------------

For summaries, you need a language model that korgalore can reach. It
can run on your machine, or it can be a hosted service. This section
uses `Ollama <https://ollama.com/>`_ on your own machine, because then
no mail leaves your computer. Any server with an OpenAI-compatible API
works the same way, and so do command-line tools like ``llm``.

.. note::
   We don't recommend a model yet. The model below is only an example.
   A bigger model writes better summaries, but it is slower. Try a few
   and compare.

1. Install Ollama and download a model:

   .. code-block:: bash

      ollama pull qwen3:32b

2. Give Ollama a big enough context window. By default it is small,
   and Ollama **cuts longer prompts without an error**, so the model
   sees only part of a thread. If Ollama runs as a systemd service:

   .. code-block:: bash

      sudo systemctl edit ollama.service

   Add these lines, then restart Ollama:

   .. code-block:: ini

      [Service]
      Environment="OLLAMA_CONTEXT_LENGTH=8192"

   If you start Ollama yourself, run
   ``OLLAMA_CONTEXT_LENGTH=8192 ollama serve`` instead.

3. Add the summarizer to your configuration, and point the digest at
   it:

   .. code-block:: toml

      [summarizers.local]
      type = 'openai'
      url = 'http://localhost:11434/v1'
      model = 'qwen3:32b'

      [deliveries.lkml-digest]
      feed = 'lkml'
      target = 'personal'
      mode = 'digest'
      summarizer = 'local'

4. See how much work the next digest is, without calling the model:

   .. code-block:: bash

      kgl digest --estimate lkml-digest

   The output looks like this:

   .. code-block:: text

      Digest lkml-digest (not due yet, counting up to now)
        412 threads, 1,034 messages
        Summarizer local, model qwen3:32b, on this machine
        No summary needed: 233, cached: 0, over max_summaries: 0
        Model calls: 179 (0 build on an earlier summary)
        Input: 1,481,233 chars, about 370,308 tokens; largest prompt 24,000 chars
        Cut to max_input_chars (24,000): 11 prompts, 197,410 chars left out

   "No summary needed" counts the new threads that nobody answered,
   except patch series: their facts already say everything. "Model calls" is how many
   summaries the model must write. If that is too many for your
   machine, set ``max_summaries`` on the delivery. The busiest threads
   then get their summaries first.

5. Send a digest now:

   .. code-block:: bash

      kgl digest --force lkml-digest

   This command returns quickly, but the digest does not arrive yet.
   The **digest worker** writes the summaries in the background, and
   then sends the digest. With a local model, this can take a long time
   for a busy list. You can follow the work in the worker's log (see
   "Where the Worker Logs" below). When ``kgl pull`` starts the worker,
   the log is ``digest-worker.log`` in the data directory:

   .. code-block:: bash

      tail -f ~/.local/share/korgalore/digest-worker.log

The second digest is much faster than the first. korgalore keeps each
summary for 30 days. When a thread gets new replies, only the new
messages and the earlier summary go to the model.

Asking for More in the Summaries
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The model gets the same instructions for every digest. If you want
to know something more about each thread, add ``summary_instructions``
to the delivery:

.. code-block:: toml

   [deliveries.lkml-digest]
   feed = 'lkml'
   target = 'personal'
   mode = 'digest'
   summarizer = 'local'
   summary_instructions = 'Tell me if anyone sounds confused or upset.'

korgalore adds your text at the end of its own instructions. Its own
rules still come first: the model writes short plain points, and never
says that a patch was reviewed or applied. The facts in the digest
(versions, trailers, counts) never come from the model, so your
instructions can't change them.

Each delivery has its own instructions, so two digests of the same
feed can ask for different things. korgalore only uses a saved summary
again when it was made with the same instructions. So after you change
them, the next digest can't build on the earlier summaries: it writes a
new summary for each thread that needs one (up to ``max_summaries``),
from the messages in that digest only. The summaries made with the old
text stay saved for up to 30 days, so changing the text back makes them
useful again.
Run ``kgl digest --estimate`` to see how many summaries the next digest
needs.

The Digest Worker
-----------------

Summaries can take hours, so ``kgl pull`` doesn't make them. For a
summarized digest, ``kgl pull`` only collects the messages, and then
starts the digest worker in the background. Meanwhile, ``kgl pull``
keeps delivering mail as usual.

* Only one worker runs at a time. If ``kgl pull`` finds a worker
  running, it doesn't start a second one.
* The worker finishes every digest that is waiting, then it exits.
* A stopped worker loses no work. The next one uses the summaries that
  were already made.
* A new digest is collected only after the last one was sent. So if the
  worker is slow, digests come late, but no messages are lost.

If you run ``kgl pull`` from a systemd service, read "Summarized
Digests and Systemd" in :doc:`usage`. systemd stops the worker together
with ``kgl pull``, so the worker needs its own service.

Where the Worker Logs
~~~~~~~~~~~~~~~~~~~~~

The log depends on who starts the worker:

* When ``kgl pull`` (or the GUI) starts it, the worker writes to
  ``digest-worker.log`` in the data directory.
* When you start it yourself with ``kgl digest --work``, for example
  from a systemd service, it logs like any other ``kgl`` command: to the
  file you give with ``-l``, and to the terminal or the journal. With the
  service from :doc:`usage`, that is ``kgl-digest.log`` in the data
  directory, and also ``journalctl --user -u korgalore-digest.service``.

Privacy
-------

Lore lists are public, so it is fine to send their messages to a hosted
service. A lei feed can include your **private mail**, so korgalore
doesn't let a lei feed use a summarizer outside your machine. See
"Privacy" under "Summarized Digests" in :doc:`configuration` for the
details and for ``allow_private_feeds``.

Troubleshooting
---------------

**The digest never arrives**

Look at the worker's log (see "Where the Worker Logs" above).

* If the log is still growing, the worker is still writing summaries.
  Wait, or set ``max_summaries`` to make digests smaller.
* If the log stops in the middle, something stopped the worker. With
  systemd, this is usually the reason; see "The Digest Worker" above.
  Run ``kgl digest --work`` to finish the digest by hand.
* With ``[digests] worker = 'external'``, nothing starts the worker for
  you. Run ``kgl digest --work`` yourself, or from its own service (see
  "Summarized Digests and Systemd" in :doc:`usage`).

You never need to delete a lock file. A worker that stopped releases its
lock by itself.

**Every thread says "Summary unavailable."**

korgalore could not reach the model. The log says why, for each thread.
After 3 failures in a row, korgalore stops calling the model until the
next run, so a server that is down doesn't make the digest wait for
hours. The digest is still sent, with all its facts.

Check that the server runs, and that ``url`` and ``model`` are right.
For Ollama, ``ollama list`` shows the model names.

**Some threads say "Summary skipped"**

The digest reached its ``max_summaries`` limit. Raise the limit, or
remove it.

**The summaries miss the end of long threads**

Look for a warning in the log that the server read only part of the
prompt. For Ollama, make ``OLLAMA_CONTEXT_LENGTH`` bigger (see step 2
above). Or lower ``max_input_chars``, so each prompt fits.

**I changed the model, and summaries are slow again**

That is expected. Summaries are saved per model, so a new model starts
again from the messages. The same happens once after a korgalore update
that changes the instructions for the model.

**I want to throw away all the saved summaries**

Stop the worker, then delete the ``summaries`` directory in the data
directory. It only saves time: the next digest makes new summaries.
