import time
from typing import List, Optional, Union

import pandas as pd
from ollama import Client
from openai import OpenAI
try:
    from google import genai
except ImportError:
    genai = None


class LLMFilter:
    """
    A class to filter articles using an LLM (Language Model) based on titles and optional keywords.

    Args:
        df (pd.DataFrame): DataFrame containing the articles to be filtered.
        client_type (str): The type of client to use ("openai", "ollama", or "gemini"). Defaults to "openai".
        model (str): The model to use for filtering. Defaults to "gpt-3.5-turbo".
        filtering_prompt (str): The prompt content for filtering the articles.
    """

    def __init__(
        self,
        df: pd.DataFrame,
        llm_provider: str = "openai",
        model: str = "gpt-3.5-turbo",
        filtering_prompt: str = "",
        llm_api_key: str = "", # 共通の引数名に変更
    ) -> None:
        """
        Initializes the LLMFilter with a DataFrame of articles and an LLM model.

        Args:
            df (pd.DataFrame): The DataFrame containing articles with their details.
            client_type (str): The type of client to use ("openai", "ollama", or "gemini"). Defaults to "openai".
            model (str): The model to use for filtering. Defaults to "gpt-3.5-turbo".
            llm_api_key (str): API key for the LLM provider.
        """
        self.df: pd.DataFrame = df
        self.llm_provider: str = llm_provider.lower()
        self.model: str = model
        self.filtering_prompt: str = filtering_prompt
        self.client: Union[OpenAI, Client, any] # Any for genai.Client
        
        if self.llm_provider == "openai":
            self.client = OpenAI(api_key=llm_api_key)
        elif self.llm_provider == "ollama":
            self.client = Client(host="http://localhost:11434")
        elif self.llm_provider == "gemini":
            if genai is None:
                raise ImportError("google-genai package is not installed. Please install it with 'pip install google-genai'.")
            self.client = genai.Client(api_key=llm_api_key)
        else:
            e = "Invalid client_type. Choose 'openai', 'ollama', or 'gemini'."
            raise ValueError(e)

    def is_relevant(
        self,
        client: Union[OpenAI, Client, any],
        filtering_prompt: str,
        title: str,
        keywords: Optional[List[str]] = None,
        model: str = "gpt-3.5-turbo",
    ) -> bool:
        """
        Determines if a publication is relevant based on its title and optional keywords using an LLM.

        Args:
            client (Union[OpenAI, Client]): The client used to interact with the API.
            filtering_prompt (str): The prompt used to instruct the LLM on relevance filtering.
            title (str): The title of the publication.
            keywords (Optional[List[str]]): A list of keywords associated with the publication. Defaults to None.
            model (str): The model to use for the API call. Defaults to "gpt-3.5-turbo".

        Returns:
            bool: True if the publication is deemed relevant, otherwise False.
        """
        if keywords:
            message = f"Title of the publication: '{title}'\nKeywords: {', '.join(keywords)}"
        else:
            message = f"Title of the publication: '{title}'"

        content = None

        if self.llm_provider == "ollama" and isinstance(client, Client):
            # Use Ollama
            response = client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": filtering_prompt},
                    {"role": "user", "content": message},
                ],
            )
            content = response["message"]["content"]
        elif self.llm_provider == "openai" and isinstance(client, OpenAI):
            # Use OpenAI API
            response = client.chat.completions.create(  # type: ignore[assignment]
                model=model,
                messages=[
                    {"role": "system", "content": filtering_prompt},
                    {"role": "user", "content": message},
                ],
            )
            # OpenAI returns an object with 'choices', Ollama does not
            content = response.choices[0].message.content  # type: ignore[attr-defined]
        elif self.llm_provider == "gemini":
            # Use Google GenAI
            prompt_text = f"{filtering_prompt}\n\nTask: Based on the title and keywords, answer 'yes' or 'no'.\n\n{message}"
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt_text
                )
                content = response.text
            except Exception as e:
                print(f"Gemini API Error: {e}")
                return False
        else:
            e = f"Invalid client type or provider mismatch: {self.llm_provider}"
            raise TypeError(e)

        if content is not None:
            return "yes" in content.lower()
        else:
            return False

    def filter_articles(self) -> pd.DataFrame:
        """
        Filters the articles in the DataFrame by determining their relevance using the LLM.

        Returns:
            pd.DataFrame: A filtered DataFrame containing only the articles deemed relevant by the LLM.
        """
        retained_indices: List[int] = []

        for index, article in self.df.iterrows():
            if self.is_relevant(
                client=self.client,
                filtering_prompt=self.filtering_prompt,
                title=article["Title"],
                keywords=article.get("Keywords"),
                model=self.model,
            ):
                retained_indices.append(index)

            time.sleep(0.5)

        # Return a DataFrame containing only the retained articles
        return self.df.loc[retained_indices]