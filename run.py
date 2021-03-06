import datasets
from transformers import AutoTokenizer, AutoModelForSequenceClassification, \
    AutoModelForQuestionAnswering, Trainer, TrainerCallback, TrainingArguments, HfArgumentParser
from helpers import prepare_dataset_nli, prepare_train_dataset_qa, \
    prepare_validation_dataset_qa, QuestionAnsweringTrainer, compute_accuracy, initialize_forgotten, \
    return_forgotten, change_find_forgettable, compute_binary_accuracy
import os
import json
import copy

NUM_PREPROCESSING_WORKERS = 2

class EvalCallback(TrainerCallback):
    # https://stackoverflow.com/questions/67457480/how-to-get-the-accuracy-per-epoch-or-step-for-the-huggingface-transformers-train
    def __init__(self, trainer):
        super().__init__()
        self.trainer = trainer
        initialize_forgotten(len(trainer.eval_dataset))

    def on_epoch_begin(self, args, state, control, **kwargs):
        self.trainer.evaluate()

class SaveCallback(TrainerCallback):
    def __init__(self, trainer):
        super().__init__()
        self.trainer = trainer
        self.epoch = 0
        initialize_forgotten(len(trainer.eval_dataset))

    def on_epoch_end(self, args, state, control, **kwargs):
        self.trainer.save_model(os.path.join(self.trainer.args.output_dir, ('epoch_' + str(state.epoch)[0:4])))

def main():
    argp = HfArgumentParser(TrainingArguments)
    # The HfArgumentParser object collects command-line arguments into an object (and provides default values for unspecified arguments).
    # In particular, TrainingArguments has several keys that you'll need/want to specify (when you call run.py from the command line):
    # --do_train
    #     When included, this argument tells the script to train a model.
    #     See docstrings for "--task" and "--dataset" for how the training dataset is selected.
    # --do_eval
    #     When included, this argument tells the script to evaluate the trained/loaded model on the validation split of the selected dataset.
    # --per_device_train_batch_size <int, default=8>
    #     This is the training batch size.
    #     If you're running on GPU, you should try to make this as large as you can without getting CUDA out-of-memory errors.
    #     For reference, with --max_length=128 and the default ELECTRA-small model, a batch size of 32 should fit in 4gb of GPU memory.
    # --num_train_epochs <float, default=3.0>
    #     How many passes to do through the training data.
    #     This number should be greater than 1 when training on forgotten examples
    # --output_dir <path>
    #     Where to put the trained model checkpoint(s) and any eval predictions.
    #     *This argument is required*.

    argp.add_argument('--model', type=str,
                      default='google/electra-small-discriminator',
                      help="""This argument specifies the base model to fine-tune.
        This should either be a HuggingFace model ID (see https://huggingface.co/models)
        or a path to a saved model checkpoint (a folder containing config.json and pytorch_model.bin).""")
    argp.add_argument('--task', type=str, choices=['nli', 'qa'], required=True,
                      help="""This argument specifies which task to train/evaluate on.
        Pass "nli" for natural language inference or "qa" for question answering.
        By default, "nli" will use the SNLI dataset, and "qa" will use the SQuAD dataset.""")
    argp.add_argument('--dataset', type=str, default=None,
                      help="""This argument overrides the default dataset used for the specified task.""")
    argp.add_argument('--max_length', type=int, default=128,
                      help="""This argument limits the maximum sequence length used during training/evaluation.
        Shorter sequence lengths need less memory and computation time, but some examples may end up getting truncated.""")
    argp.add_argument('--max_train_samples', type=int, default=None,
                      help='Limit the number of examples to train on.')
    argp.add_argument('--max_eval_samples', type=int, default=None,
                      help='Limit the number of examples to evaluate on.')
    argp.add_argument('--num_forgotten_epochs', type=float, default=0,
                      help='whether or not to train on forgotten examples')
    argp.add_argument('--compute_binary_accuracy', type=str, choices=['True', 'False'], default='False',
                      help='non-entailment samples only counted wrong if classified as entailment')

    training_args, args = argp.parse_args_into_dataclasses()
    # uncomment this line when training small datasets on cpu
    # training_args.logging_steps = 9
    

   
    # a line like: training_args.num_train_epochs = 0.005 can modify the number of train epochs we have    
    # num_train_epochs is stored in trainig_args directly as num_train_epochs
    # have separate boolean/argument that will be train_forgotten
    # try to copy out eval_dataset part into separate method that we can call when training forgotten
    # loop through epochs, save model at different output_dir every epoch so we can pick up training there

    # Dataset selection
    if args.dataset.endswith('.json') or args.dataset.endswith('.jsonl'):
        dataset_id = None
        # Load from local json/jsonl file
        # add in download_mode='force_redownload' if running with hypothesis-only nli
        dataset = datasets.load_dataset('json', data_files=args.dataset)
        # By default, the "json" dataset loader places all examples in the train split,
        # so if we want to use a jsonl file for evaluation we need to get the "train" split
        # from the loaded dataset
        eval_split = 'train'
    else:
        default_datasets = {'qa': ('squad',), 'nli': ('snli',)}
        dataset_id = tuple(args.dataset.split(':')) if args.dataset is not None else \
            default_datasets[args.task]
        # MNLI has two validation splits (one with matched domains and one with mismatched domains). Most datasets just have one "validation" split
        eval_split = 'validation_matched' if dataset_id == ('glue', 'mnli') else 'validation'
        # Load the raw data
        # add in download_mode='force_redownload' if running with hypothesis-only nli
        dataset = datasets.load_dataset(*dataset_id)
    
    # NLI models need to have the output label count specified (label 0 is "entailed", 1 is "neutral", and 2 is "contradiction")
    task_kwargs = {'num_labels': 3} if args.task == 'nli' else {}

    # Here we select the right model fine-tuning head
    model_classes = {'qa': AutoModelForQuestionAnswering,
                     'nli': AutoModelForSequenceClassification}
    model_class = model_classes[args.task]
    # Initialize the model and tokenizer from the specified pretrained model/checkpoint
    model = model_class.from_pretrained(args.model, **task_kwargs)
    tokenizer = AutoTokenizer.from_pretrained(args.model, use_fast=True)

    # Select the dataset preprocessing function (these functions are defined in helpers.py)
    if args.task == 'qa':
        prepare_train_dataset = lambda exs: prepare_train_dataset_qa(exs, tokenizer)
        prepare_eval_dataset = lambda exs: prepare_validation_dataset_qa(exs, tokenizer)
    elif args.task == 'nli':
        prepare_train_dataset = prepare_eval_dataset = \
            lambda exs: prepare_dataset_nli(exs, tokenizer, args.max_length)
        # prepare_eval_dataset = prepare_dataset_nli
    else:
        raise ValueError('Unrecognized task name: {}'.format(args.task))

    print("Preprocessing data... (this takes a little bit, should only happen once per dataset)")
    if dataset_id == ('snli',):
        # remove SNLI examples with no label
        dataset = dataset.filter(lambda ex: ex['label'] != -1)
    
    train_dataset = None
    eval_dataset = None
    train_dataset_featurized = None
    eval_dataset_featurized = None
    if training_args.do_train:
        train_dataset = eval_dataset = dataset['train']
        if args.max_train_samples:
            train_dataset = train_dataset.select(range(args.max_train_samples))
        train_dataset_featurized = train_dataset.map(
            prepare_train_dataset,
            batched=True,
            num_proc=NUM_PREPROCESSING_WORKERS,
            remove_columns=train_dataset.column_names
        )

        # set eval dataset to the same thing as train dataset 
        eval_dataset_featurized = copy.deepcopy(train_dataset_featurized)
        
    if training_args.do_eval:
        eval_dataset = dataset[eval_split]
        if args.max_eval_samples:
            eval_dataset = eval_dataset.select(range(args.max_eval_samples))
        eval_dataset_featurized = eval_dataset.map(
            prepare_eval_dataset,
            batched=True,
            num_proc=NUM_PREPROCESSING_WORKERS,
            remove_columns=eval_dataset.column_names
        )

    # Select the training configuration
    trainer_class = Trainer
    eval_kwargs = {}
    # If you want to use custom metrics, you should define your own "compute_metrics" function.
    # For an example of a valid compute_metrics function, see compute_accuracy in helpers.py.
    compute_metrics = None
    if args.task == 'qa':
        # For QA, we need to use a tweaked version of the Trainer (defined in helpers.py)
        # to enable the question-answering specific evaluation metrics
        trainer_class = QuestionAnsweringTrainer
        eval_kwargs['eval_examples'] = eval_dataset
        metric = datasets.load_metric('squad')
        compute_metrics = lambda eval_preds: metric.compute(
            predictions=eval_preds.predictions, references=eval_preds.label_ids)
    elif args.task == 'nli':
        if args.compute_binary_accuracy == 'True':
            compute_metrics = compute_binary_accuracy
        else:
            compute_metrics = compute_accuracy

    

    # This function wraps the compute_metrics function, storing the model's predictions
    # so that they can be dumped along with the computed metrics
    eval_predictions = None
    def compute_metrics_and_store_predictions(eval_preds):
        nonlocal eval_predictions
        eval_predictions = eval_preds
        return compute_metrics(eval_preds)

    # Initialize the Trainer object with the specified arguments and the model and dataset we loaded above
    trainer = trainer_class(
        model=model,
        args=training_args,
        train_dataset=train_dataset_featurized,
        eval_dataset=eval_dataset_featurized,
        tokenizer=tokenizer,
        compute_metrics=compute_metrics_and_store_predictions
    )
    if args.num_forgotten_epochs != 0:
        eval_callback = EvalCallback(trainer)
        trainer.add_callback(eval_callback)

    save_callback = SaveCallback(trainer)
    trainer.add_callback(save_callback)
    
    # Train and/or evaluate
    if training_args.do_train:
        trainer.train()
        trainer.save_model()
        trainer.evaluate()

        # If you want to customize the way the loss is computed, you should subclass Trainer and override the "compute_loss"
        # method (see https://huggingface.co/transformers/_modules/transformers/trainer.html#Trainer.compute_loss).
        #
        # You can also add training hooks using Trainer.add_callback:
        #   See https://huggingface.co/transformers/main_classes/trainer.html#transformers.Trainer.add_callback
        #   and https://huggingface.co/transformers/main_classes/callback.html#transformers.TrainerCallback

    if args.num_forgotten_epochs != 0:
        # obtain indices of forgotten examples
        change_find_forgettable(False)
        forgotten_indices = return_forgotten()

        # write forgotten examples to json file
        with open(os.path.join(training_args.output_dir, 'forgotten_examples.jsonl'), encoding='utf-8', mode='w') as f:
            for i, example in enumerate(eval_dataset):
                if(i in forgotten_indices):
                    example_with_prediction = dict(example)
                    f.write(json.dumps(example_with_prediction))
                    f.write('\n')

        # create new dataset consisting only of forgotten examples
        forgotten_dataset = trainer.eval_dataset.select(forgotten_indices)
        forget_args = copy.deepcopy(training_args)

        # Uncomment if training with small dataset on cpu
        # forget_args.logging_steps = 3

        # Save output of args to subfolder
        forget_args.output_dir = os.path.join(training_args.output_dir, 'forgotten')
        forget_args.num_train_epochs = args.num_forgotten_epochs

        # create new forget trainer that picks up where original trainer left off and trains
        # only on forgotten examples
        forget_model = model_class.from_pretrained(training_args.output_dir, **task_kwargs)
        forget_tokenizer = AutoTokenizer.from_pretrained(args.model, use_fast=True)
        forget_trainer = trainer_class(
            model=forget_model,
            args=forget_args,
            train_dataset=forgotten_dataset,
            eval_dataset=forgotten_dataset,
            tokenizer=forget_tokenizer,
            compute_metrics=compute_metrics_and_store_predictions
        )
        forget_eval_callback = EvalCallback(forget_trainer)
        forget_trainer.add_callback(forget_eval_callback)
        forget_save_callback = SaveCallback(forget_trainer)
        forget_trainer.add_callback(forget_save_callback)
        
        forget_trainer.train()
        forget_trainer.save_model()

    if training_args.do_eval:
        results = trainer.evaluate(**eval_kwargs)

        # To add custom metrics, you should replace the "compute_metrics" function (see comments above).
        #
        # If you want to change how predictions are computed, you should subclass Trainer and override the "prediction_step"
        # method (see https://huggingface.co/transformers/_modules/transformers/trainer.html#Trainer.prediction_step).
        # If you do this your custom prediction_step should probably start by calling super().prediction_step and modifying the
        # values that it returns.

        print('Evaluation results:')
        print(results)

        os.makedirs(training_args.output_dir, exist_ok=True)

        with open(os.path.join(training_args.output_dir, 'eval_metrics.json'), encoding='utf-8', mode='w') as f:
            json.dump(results, f)

        with open(os.path.join(training_args.output_dir, 'eval_predictions.jsonl'), encoding='utf-8', mode='w') as f:
            if args.task == 'qa':
                predictions_by_id = {pred['id']: pred['prediction_text'] for pred in eval_predictions.predictions}
                for example in eval_dataset:
                    example_with_prediction = dict(example)
                    example_with_prediction['predicted_answer'] = predictions_by_id[example['id']]
                    f.write(json.dumps(example_with_prediction))
                    f.write('\n')
            else:
                for i, example in enumerate(eval_dataset):
                    example_with_prediction = dict(example)
                    example_with_prediction['predicted_scores'] = eval_predictions.predictions[i].tolist()
                    example_with_prediction['predicted_label'] = int(eval_predictions.predictions[i].argmax())
                    f.write(json.dumps(example_with_prediction))
                    f.write('\n')


if __name__ == "__main__":
    main()
