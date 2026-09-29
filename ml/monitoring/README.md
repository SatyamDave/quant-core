# ml/monitoring

`psi` computes the population stability index of live features against training features. `demote_on_drift` records `no_signal` automatically when any feature's PSI exceeds 0.25. `promote` refuses unless a `HumanApproval` record for that exact artifact is supplied and the model is a registered challenger. Prediction drift and realized-versus-expected edge are later work.
