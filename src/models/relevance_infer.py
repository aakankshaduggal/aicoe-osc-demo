import json
import logging
import os
import re
from abc import abstractmethod, ABC
from pathlib import Path
import sys
import importlib

import pandas as pd
from farm.infer import Inferencer

import src.components.utils.kpi_mapping as kpi_mapping

_logger = logging.getLogger(__name__)


class BaseRelevanceInfer(ABC):
    """ An abstract base class for predicting relevant data for given question(s).
    The `run_folder` is the main method for this
    class and its children.

    Args:
        infer_config: An instance of model_pipeline.config.InferConfig class"""

    def __init__(self, infer_config):
        self.infer_config = infer_config
        self.data_type = self._get_data_type()
        importlib.reload(kpi_mapping)

        # Questions can be set in the config file. If not provided, the prediction will be made for all KPI questions
        if len(self.infer_config.kpi_questions) > 0:
            self.questions = self.infer_config.kpi_questions
        else:
            # Filter KPIs based on section and whether they can be found in text or table.
            self.questions = [
                q_text		  
                for q_id, (q_text, sect) in kpi_mapping.KPI_MAPPING_MODEL.items()
                if len(set(sect).intersection(set(self.infer_config.sectors))) > 0 and self.data_type.upper() in kpi_mapping.KPI_CATEGORY[q_id]
            ]

        self.result_dir = self.infer_config.result_dir[self.data_type]
        if not os.path.exists(self.result_dir):
            os.makedirs(self.result_dir)

        farm_logger = logging.getLogger('farm')
        farm_logger.setLevel(self.infer_config.farm_infer_logging_level)
        self.model = Inferencer.load(
            self.infer_config.load_dir[self.data_type],
            batch_size=self.infer_config.batch_size,
            gpu=self.infer_config.gpu,
            num_processes=self.infer_config.num_processes,
            disable_tqdm=self.infer_config.disable_tqdm
        )

    def run_folder(self):
        """The method is responsible for making prediction on all the data
        (csv files or json) inside a folder and save the relevant tables or
        paragraphs for questions inside a csv file.
        """
        all_text_path_dict = self._gather_extracted_files()
        df_list = []
        num_pdfs = len(all_text_path_dict)
        _logger.info("{} Starting Relevence Inference for the following extracted pdf files found in {}:\n{} ".
                     format("#" * 20, self.result_dir, [pdf for pdf in all_text_path_dict.keys()]))
        for i, (pdf_name, file_path) in enumerate(all_text_path_dict.items()):
            _logger.info("{} {}/{} PDFs".format("#" * 20, i+1, num_pdfs))
            predictions_file_name = "{}_{}".format(pdf_name, "predictions_relevant.csv")
            if self.infer_config.skip_processed_files and predictions_file_name in os.listdir(self.result_dir):
                _logger.info("The relevance infer results for {} already exists. Skipping.".format(pdf_name))
                _logger.info(
                    "If you would like to re-process the already processed files, set "
                    "`skip_processed_files` to False in the config file. "
                )
                continue
            _logger.info("Running inference for {}:".format(pdf_name))

            try:
                data = self._gather_data(pdf_name, file_path)
                num_data_points = len(data)
                predictions = []
                chunk_size = 1000
                chunk_idx = 0
                while chunk_idx * chunk_size < num_data_points:
                    data_chunk = data[chunk_idx * chunk_size: (chunk_idx + 1) * chunk_size]
                    predictions_chunk = self.model.inference_from_dicts(dicts=data_chunk)
                    predictions.extend(predictions_chunk)
                    chunk_idx += 1
                flat_predictions = [
                    example for batch in predictions for example in batch["predictions"]
                ]
                positive_examples = [
                    data[index]
                    for index, pred_example in enumerate(flat_predictions)
                    if pred_example["label"] == "1"
                ]
                df = pd.DataFrame(positive_examples)
                df["source"] = self.data_type

                df_list.append(df)
                predictions_file_path = os.path.join(
                    self.result_dir, predictions_file_name

                )
                df.to_csv(predictions_file_path)
                _logger.info(
                    "Saved {} relevant {} examples for {} in {}".format(
                        len(df), self.data_type, pdf_name, predictions_file_path
                    )
                )
            except:
                e = sys.exc_info()[0]
                _logger.warning("There was an error making inference (RELEVANCE) on {}".format(pdf_name))
                _logger.warning("The error is\n{}\nSkipping this pdf".format(e))

        concatenated_dfs = pd.concat(df_list) if len(df_list) > 0 else pd.DataFrame()
        self.model.close_multiprocessing_pool()
        return concatenated_dfs

    @abstractmethod
    def _get_data_type(self):
        """Force child classes to provide data type."""
        return None

    @abstractmethod
    def _gather_data(self):
        """An abstract method provided by the child class. This method is
        responsible for getting all the data inside a folder and returning a list
        of examples for text-pair classification. Therefore, each example must
        have "text" and "text_b" keys.
        """
        return None

    @abstractmethod
    def _gather_extracted_files(self):
        pass


class TableRelevanceInfer(BaseRelevanceInfer):
    """This class is responsible for finding relevant tables to given questions.
    Args:
        infer_config (obj of model_pipeline.config.InferConfig)
        """

    def __init__(self, infer_config):
        super(TableRelevanceInfer, self).__init__(infer_config)

    def _get_data_type(self):
        return "Table"

    def _gather_extracted_files(self):
        """
        Gathers all the extracted tables saved as CSV files for each pdf
        Returns:
            table_paths_dicts(dict): A dictionary where the keys are the pdf names and the values are list of
            CSV paths for each pdf
        """
        all_table_paths = sorted(Path(self.infer_config.extracted_dir).rglob("*.csv"))
        table_paths_dicts = {}
        for table_path in all_table_paths:
            pdf_name = os.path.basename(table_path).split("_page")[0]
            if pdf_name in table_paths_dicts:
                table_paths_dicts[pdf_name].append(table_path)
            else:
                table_paths_dicts[pdf_name] = [table_path]
        return table_paths_dicts

    def _gather_data(self, pdf_name, table_paths):
        """ Gathers all the table data for the given pdf and prepares them to be passed to pass to table model
        Args:
            pdf_name (str): Name of the pdf
            pdf_path (str): Path to the pdf
        Returns:
            table_data (A list of a list of dicts): The dict has "page", "pdf_name",
                                                    "text", "text_b" keys.

        """
        all_table_texts = list(map(self.gather_text_from_table, table_paths))
        # Filter tables that do not contain any text

        all_table_texts_and_paths = [
            (example, path)
            for example, path in zip(all_table_texts, table_paths)
            if re.search(r"\w+", example) is not None
        ]
        table_data = []
        for kpi_question in self.questions:
            # build combinations of each data point and each question
            # Keep track of page number which the table is extracted from and the pdf it belongs to.
            table_data.extend(
                [
                    {
                        "page": re.search(r"page(\d*)", str(tb_path)).group(1),
                        "pdf_name": pdf_name,
                        "text": kpi_question,
                        "text_b": tb_text,
                        "csv_file": os.path.basename(tb_path),
                    }
                    for tb_text, tb_path in all_table_texts_and_paths
                ]
            )
        _logger.info(
            "###### Received {} examples for Table, number of questions: {}".format(
                len(all_table_texts_and_paths), len(self.questions)
            )
        )
        return table_data

    @staticmethod
    def gather_text_from_table(file):
        """Extract all the text inside a csv file
        Args:
            file (str): Path to a csv file
        Return:
            (A str): Texts inside all cells which contain text data.
        """
        table = pd.read_csv(file, index_col=0)
        text = []
        columns = list(table)
        for column in columns:
            column_proces = table[column].dropna().astype(str)
            numbers = column_proces.str.findall(r"^\W*[0-9]*\W?[0-9]*?\W*$")
            not_numbers = numbers.apply(lambda x: False if len(x) > 0 else True)
            not_numbers_idx = not_numbers[not_numbers == True].index
            column_text = [text for text in column_proces[not_numbers_idx]]
            text += column_text
        return ", ".join(text)


class TextRelevanceInfer(BaseRelevanceInfer):
    """This class is responsible for finding relevant texts to given questions.
        Args:
            infer_config (obj of model_pipeline.config.InferConfig)
    """

    def __init__(self, infer_config):
        super(TextRelevanceInfer, self).__init__(infer_config)

    def _get_data_type(self):
        return "Text"

    def _gather_extracted_files(self):
        """
        Gathers all the extracted texts for each pdf
        Returns:
            A dictionary where the keys are the pdf names and the values are the path to the json files containing
            the extracted text for each pdf
        """
        # Get all the json extracted from pdfs which are located in extracted folder
        text_paths = sorted(Path(self.infer_config.extracted_dir).rglob("*.json"))
        return {
            os.path.splitext(os.path.basename(file_path))[0]: file_path
            for file_path in text_paths
            if "table_meta" not in str(file_path)
        }

    def _gather_data(self, pdf_name, pdf_path):
        """ Gathers all the text data inside the given pdf and prepares it to be passed to text model
        Args:
            pdf_name (str): Name of the pdf
            pdf_path (str): Path to the pdf
        Returns:
            text_data (A list of a list of dicts): The dict has "page", "pdf_name",
                                                    "text", "text_b" keys.
        """
        # Get all the extracted pdf text from the json file in the extracted folder
        pdf_content = self.read_text_from_json(pdf_path)
        text_data = []
        # build all possible combinations of paragraphs and  questions
        # Keep track of page number which the text is extracted from and the pdf it belongs to.
        for kpi_question in self.questions:
            text_data.extend(
                [
                    {
                        "page": page_num,
                        "pdf_name": pdf_name,
                        "text": kpi_question,
                        "text_b": paragraph,
                    }
                    for page_num, page_content in pdf_content.items()
                    for paragraph in page_content
                ]
            )

        _logger.info(
            "###### Received {} examples for Text, number of questions: {}".format(
                int(len(text_data) / len(self.questions)), len(self.questions)
            )
        )

        return text_data

    @staticmethod
    def read_text_from_json(file):
        with open(file) as f:
            text = json.load(f)
            return text

    def run_text(self, input_text, input_question):
        """ A method to make prediction on relevancy of a input_text and input_questions"""
        basic_texts = [
            {"text": input_question, "text_b": input_text},
        ]
        predictions = self.model.inference_from_dicts(dicts=basic_texts)
        return predictions
